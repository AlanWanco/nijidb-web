"""Incremental music monitoring inside the existing news worker process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import time
import traceback
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from bs4 import BeautifulSoup

from app.music import (
    RELEASE_ID_PATTERN,
    SOURCE_URL,
    music_content_signature,
    parse_music_listing,
    parse_release,
)
from app.news_fetch import (
    OFFICIAL_BROWSER_HEADERS,
    image_dimensions_from_bytes,
    is_allowed_official_image_url,
)
from scripts.local_official_news_poller import (
    OfficialSiteBlocked,
    PollerConfig,
    PollerConfigError,
    SiteApiError,
    _atomic_write_text,
    _read_state_bytes,
    direct_api_client,
    image_signature,
    log,
    official_news_client,
    read_response_bytes,
)
from scripts.news_network import pin_r2_client_to_ipv4
from scripts.nijidb_bot_events import (
    flush_bot_event_outbox,
    flush_site_event_outbox,
    mark_site_event_imported,
    queue_site_event,
    send_poller_heartbeat,
)

MUSIC_STATE_VERSION = 1
MUSIC_STATE_MAX_BYTES = 4 * 1024 * 1024
MUSIC_API_PATH = "/api/ingest/music-worker"
MUSIC_HTML_MAX_BYTES = 8 * 1024 * 1024


def default_music_state() -> dict[str, Any]:
    return {
        "version": MUSIC_STATE_VERSION,
        "initialized": False,
        "releases": {},
        "stats": {"passes": 0, "submitted": 0, "seeded": 0, "skipped": 0, "errors": 0},
        "backoff_until": 0.0,
        "last_pass_at": 0.0,
    }


def music_state_path(config: PollerConfig) -> Path:
    return config.music_state_file or config.state_file.with_name("music-poller.json")


def load_music_state(path: Path) -> dict[str, Any]:
    state = default_music_state()
    if not path.exists() and not path.is_symlink():
        return state
    raw = _read_state_bytes(path)
    if raw is None:
        raise PollerConfigError("音乐状态文件不可读、过大或不是普通文件")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PollerConfigError("音乐状态文件损坏，停止监控以保留基线") from exc
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != MUSIC_STATE_VERSION
    ):
        raise PollerConfigError("音乐状态文件版本不支持")
    if not isinstance(value.get("releases"), dict) or not isinstance(
        value.get("stats"), dict
    ):
        raise PollerConfigError("音乐状态结构无效")
    state["initialized"] = value.get("initialized") is True
    for key, item in value["releases"].items():
        if not RELEASE_ID_PATTERN.fullmatch(key) or not isinstance(item, dict):
            raise PollerConfigError("音乐状态中的发行无效")
        state["releases"][key] = item
    for key, count in value["stats"].items():
        if type(count) is int and 0 <= count <= 2**63 - 1:
            state["stats"][key] = count
    for key in ("backoff_until", "last_pass_at"):
        try:
            number = float(value.get(key, 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise PollerConfigError("音乐状态时间无效") from exc
        if not math.isfinite(number) or number < 0:
            raise PollerConfigError("音乐状态时间无效")
        state[key] = number
    return state


def save_music_state(path: Path, state: dict[str, Any]) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise PollerConfigError("音乐状态路径不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > MUSIC_STATE_MAX_BYTES:
        raise PollerConfigError("音乐状态文件超过 4 MB")
    _atomic_write_text(path, content)


def public_music_cover(url: Any, config: PollerConfig, record_id: str = "") -> bool:
    """Recognize both legacy images/<id>.jpg and content-addressed music covers."""
    candidate = str(url or "").strip()
    base = str(config.r2_public_base_url or "").rstrip("/")
    if not candidate or not base:
        return False
    try:
        parsed, base_parts = urlparse(candidate), urlparse(base)
        parsed.port
        decoded = unquote(parsed.path)
        root = (
            unquote(base_parts.path).strip("/").split("/")
            if base_parts.path.strip("/")
            else []
        )
        root += config.r2_prefix.split("/") if config.r2_prefix else []
        parts = decoded.lstrip("/").split("/")
        if (
            parsed.scheme != base_parts.scheme
            or parsed.netloc != base_parts.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "?" in candidate
            or "#" in candidate
            or "\\" in decoded
            or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in candidate + decoded
            )
            or any(part in {"", ".", ".."} for part in parts)
            or parts[: len(root)] != root
        ):
            return False
    except (TypeError, ValueError):
        return False
    relative = parts[len(root) :]
    if len(relative) == 1:
        filename = Path(relative[0])
        known_name = bool(record_id) and (
            filename.stem == record_id
            or re.fullmatch(rf"{re.escape(record_id)}-[a-f0-9]{{12,64}}", filename.stem)
        )
        return known_name and filename.suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".avif",
            ".gif",
        }
    return (
        len(relative) == 2 and relative[0] == "music" and bool(Path(relative[1]).suffix)
    )


def music_row_to_api(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: str(record.get(key) or "")
        for key in (
            "id",
            "title",
            "subtitle",
            "artist",
            "release_date",
            "price",
            "cover_url",
            "cover_source_url",
            "detail_html",
            "source_url",
        )
    }
    for key, source, default, kind in (
        ("tracks", "tracks_json", "[]", list),
        ("specs", "spec_json", "{}", dict),
        ("extras", "extras_json", "[]", list),
    ):
        payload[key] = json.loads(record.get(source) or default)
        if not isinstance(payload[key], kind):
            raise ValueError("音乐发行 JSON 字段类型无效")
    return payload


def rewrite_cover(
    record: dict[str, Any], source: str, public_url: str
) -> dict[str, Any]:
    result = dict(record)
    result["cover_url"] = public_url
    soup = BeautifulSoup(record.get("detail_html") or "", "html.parser")
    for image in soup.select("img[src]"):
        if urljoin(SOURCE_URL, str(image["src"])) == source:
            image["src"] = public_url
            image.attrs.pop("srcset", None)
            image["loading"] = "lazy"
    result["detail_html"] = str(soup)
    return result


class MusicPoller:
    def __init__(self, config: PollerConfig):
        self.config = config
        self.state_file = music_state_path(config)
        if self.state_file.resolve() == config.state_file.resolve():
            raise PollerConfigError("音乐状态路径不能与新闻状态相同")
        self.state = default_music_state()
        self.s3 = None
        if not config.dry_run:
            if not config.music_api_key:
                raise PollerConfigError("音乐监控缺少 NIJIDB_MUSIC_POLLER_API_KEY")
            self.s3 = boto3.client(
                "s3",
                endpoint_url=config.r2_endpoint,
                aws_access_key_id=config.r2_access_key_id,
                aws_secret_access_key=config.r2_secret_access_key,
                region_name="auto",
                config=Config(
                    connect_timeout=10,
                    read_timeout=45,
                    retries={"mode": "standard", "max_attempts": 2},
                ),
            )
            pin_r2_client_to_ipv4(self.s3)

    def save(self) -> None:
        if not self.config.dry_run:
            save_music_state(self.state_file, self.state)

    async def official_response(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs
    ) -> httpx.Response:
        async with client.stream(
            method, url, follow_redirects=False, **kwargs
        ) as response:
            if response.status_code in {401, 403, 429}:
                raise OfficialSiteBlocked()
            if 300 <= response.status_code < 400 and response.status_code != 304:
                raise ValueError("官网音乐请求不允许重定向")
            if response.status_code != 304:
                response.raise_for_status()
            data = await read_response_bytes(response, MUSIC_HTML_MAX_BYTES)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=data,
                request=response.request,
            )

    async def get_catalog(
        self, client: httpx.AsyncClient, cycle_lock=None
    ) -> tuple[list, dict, set]:
        guard = cycle_lock if cycle_lock is not None else nullcontext()
        async with guard:
            response = await self.official_response(
                client, "GET", SOURCE_URL, headers=OFFICIAL_BROWSER_HEADERS
            )
        entries, missing = parse_music_listing(response.text)
        if self.config.limit:
            entries = entries[: self.config.limit]
            selected = {entry[0] for entry in entries}
            missing = [release_id for release_id in missing if release_id in selected]
        dynamic, failed = {}, set()
        for release_id in missing:
            await asyncio.sleep(self.config.music_detail_delay)
            try:
                async with guard:
                    response = await self.official_response(
                        client,
                        "POST",
                        urljoin(SOURCE_URL, "cd_detail.php"),
                        json=release_id.removeprefix("cd"),
                        headers=OFFICIAL_BROWSER_HEADERS,
                    )
                value = response.json()
                if not isinstance(value, str):
                    raise ValueError("官网音乐详情格式无效")
                detail = BeautifulSoup(value, "html.parser")
                if not detail.select_one(".title"):
                    raise ValueError("官网音乐详情缺少标题")
                dynamic[release_id] = detail
            except OfficialSiteBlocked:
                raise
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                failed.add(release_id)
                log(
                    f"音乐详情读取失败：{release_id}（{type(exc).__name__}），保留旧资料"
                )
        return entries, dynamic, failed

    async def api_json(
        self, api: httpx.AsyncClient, method: str, path: str, **kwargs
    ) -> dict[str, Any]:
        caller_headers = kwargs.pop("headers", {})
        headers = {
            **({"X-Nijidb-API-Key": self.config.music_api_key} if path.startswith(MUSIC_API_PATH) else {}),
            **caller_headers,
        }
        async with api.stream(
            method,
            self.config.base_url + path,
            headers=headers,
            **kwargs,
        ) as response:
            if response.status_code in {401, 403, 404, 503}:
                raise SiteApiError(
                    f"音乐接口未就绪或鉴权失败（HTTP {response.status_code}）"
                )
            response.raise_for_status()
            value = json.loads(
                await read_response_bytes(response, MUSIC_HTML_MAX_BYTES)
            )
        if not isinstance(value, dict):
            raise SiteApiError("音乐接口响应格式无效")
        return value

    async def load_site_releases(
        self, api: httpx.AsyncClient
    ) -> dict[str, dict[str, Any]]:
        data = await self.api_json(api, "GET", "/api/releases")
        rows = data.get("releases")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not row.get("id") for row in rows
        ):
            raise SiteApiError("站点音乐目录格式无效")
        return {row["id"]: row for row in rows}

    async def fetch_cover(
        self, client: httpx.AsyncClient, url: str, saved: dict
    ) -> tuple[bytes | None, dict]:
        if not is_allowed_official_image_url(url):
            raise ValueError("音乐封面不在允许的官网图片源内")
        headers = {"Accept": "image/*", "Referer": SOURCE_URL}
        if saved.get("cover_source_url") == url and saved.get("cover_sha256"):
            for header, field in (
                ("If-None-Match", "etag"),
                ("If-Modified-Since", "last_modified"),
            ):
                if saved.get(field):
                    headers[header] = saved[field]
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=False
        ) as response:
            if response.status_code in {401, 403, 429}:
                raise OfficialSiteBlocked()
            if response.status_code == 304:
                if not saved.get("cover_sha256"):
                    raise ValueError("音乐封面首次读取不能返回 304")
                return None, {
                    key: saved.get(key, "")
                    for key in ("cover_sha256", "etag", "last_modified")
                }
            if 300 <= response.status_code < 400:
                raise ValueError("音乐封面不允许重定向")
            response.raise_for_status()
            data = await read_response_bytes(
                response, self.config.music_image_max_bytes
            )
            self.validate_image(data)
            return data, {
                "cover_sha256": hashlib.sha256(data).hexdigest(),
                "etag": response.headers.get("etag", ""),
                "last_modified": response.headers.get("last-modified", ""),
            }

    def validate_image(self, data: bytes) -> tuple[str, str]:
        signature, size = image_signature(data), image_dimensions_from_bytes(data)
        if (
            not signature
            or not size
            or len(data) > self.config.music_image_max_bytes
            or size[0] < 240
            or size[1] < 120
            or max(size) > 10000
        ):
            raise ValueError("音乐封面格式、尺寸或大小无效")
        return signature

    def archive_cover(self, data: bytes) -> str:
        extension, content_type = self.validate_image(data)
        digest = hashlib.sha256(data).hexdigest()
        key = "/".join(
            part
            for part in (self.config.r2_prefix, "music", digest + extension)
            if part
        )
        try:
            exists = self.s3.head_object(Bucket=self.config.r2_bucket, Key=key).get(
                "ContentLength"
            ) == len(data)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise
            exists = False
        if not exists:
            self.s3.put_object(
                Bucket=self.config.r2_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl="public, max-age=31536000, immutable",
                Metadata={"sha256": digest},
            )
        return f"{self.config.r2_public_base_url}/{quote(key, safe='/')}"

    async def process_release(
        self,
        official: httpx.AsyncClient,
        api: httpx.AsyncClient,
        record: dict,
        previous: dict | None,
        cycle_lock=None,
    ) -> str:
        guard = cycle_lock if cycle_lock is not None else nullcontext()
        release_id = record["id"]
        saved = self.state["releases"].get(release_id, {})
        signature = music_content_signature(record)
        source = record["cover_url"]
        current_cover = (previous or {}).get("cover_url", "")
        first_catalog_baseline = bool(
            self.config.bot_event_url
            and not self.state["initialized"]
            and (
                previous is None
                or not saved
                or saved.get("source_signature") != signature
                or saved.get("baseline_pending")
            )
        ) or bool(
            saved.get("baseline_pending")
            or (not self.state["initialized"] and previous is None)
        )
        is_bot_baseline = bool(
            self.config.bot_event_url
            and saved.get("source_signature") == signature
            and (
                saved.get("bot_baseline_pending")
                or not saved.get("bot_baseline_seeded")
                or saved.get("baseline_pending")
            )
        )
        if is_bot_baseline and saved.get("source_signature") != signature:
            first_catalog_baseline = True
        if is_bot_baseline and previous is not None:
            # 站点已有且内容未变：只补 Bot 对比快照，不重新导入，避免重复下载封面。
            snapshot = {
                "id": release_id,
                "title": previous.get("title") or "",
                "subtitle": previous.get("subtitle") or "",
                "artist": previous.get("artist") or "",
                "release_date": previous.get("release_date") or "",
                "price": previous.get("price") or "",
                "cover_url": current_cover,
                "source_url": previous.get("source_url") or "",
                "tracks": json.loads(previous.get("tracks_json") or "[]"),
                "specs": json.loads(previous.get("spec_json") or "{}"),
                "extras": json.loads(previous.get("extras_json") or "[]"),
                "detail_html": previous.get("detail_html") or "",
            }
            event_id = queue_site_event(
                self.config,
                source="music",
                item_id=release_id,
                change="baseline",
                snapshot=snapshot,
                previous_id=str(previous.get("updated_at") or ""),
                site_path=MUSIC_API_PATH,
                site_payload={"release": music_row_to_api(previous), "baseline": True},
            )
            mark_site_event_imported(self.config, event_id, changed=False)
            await flush_bot_event_outbox(self.config, api)
            self.state["releases"][release_id] = {
                **saved,
                "bot_baseline_pending": False,
                "bot_baseline_seeded": True,
                "checked_at": time.time(),
            }
            self.state["stats"]["seeded"] += 1
            return "seeded"
        if previous is not None and (saved.get("baseline_pending") or not saved) and not is_bot_baseline:
            if not self.config.bot_event_url:
                # Seed known records on startup or after state loss; never rewrite all historical releases.
                image_state = {}
                if source and not self.config.dry_run:
                    async with guard:
                        _data, image_state = await self.fetch_cover(official, source, {})
                self.state["releases"][release_id] = {
                    **image_state,
                    "source_signature": signature,
                    "cover_source_url": source,
                    "cover_url": current_cover,
                    "seeded": True,
                    "checked_at": time.time(),
                }
                return "seeded"
            first_catalog_baseline = True
        if self.config.dry_run:
            return (
                "changed" if saved.get("source_signature") != signature else "skipped"
            )
        data, image_state = None, {}
        if source:
            async with guard:
                data, image_state = await self.fetch_cover(official, source, saved)
        content_changed = saved.get("source_signature") != signature
        bytes_changed = bool(
            saved.get("cover_sha256")
            and image_state.get("cover_sha256") != saved["cover_sha256"]
        )
        archived = public_music_cover(current_cover, self.config, release_id)
        if (
            saved
            and not content_changed
            and not bytes_changed
            and archived
            and current_cover == saved.get("cover_url")
            and not first_catalog_baseline
            and not is_bot_baseline
        ):
            saved.update(image_state, checked_at=time.time())
            return "skipped"
        if archived and saved.get("cover_source_url") == source and not bytes_changed:
            cover_url = current_cover
        elif source:
            if (
                data is None
            ):  # Conditional 304 but the site lost its cover; re-fetch before uploading.
                async with guard:
                    data, image_state = await self.fetch_cover(official, source, {})
            cover_url = await asyncio.to_thread(self.archive_cover, data)
        else:
            cover_url = current_cover if archived else ""
        outgoing = rewrite_cover(record, source, cover_url)
        outgoing["cover_source_url"] = source
        site_payload = {
            "release": music_row_to_api(outgoing),
            "baseline": first_catalog_baseline,
        }
        pending_event_id = ""
        if self.config.bot_event_url and (
            first_catalog_baseline
            or saved.get("source_signature") != signature
            or is_bot_baseline
        ):
            snapshot = {
                "id": release_id,
                "title": record.get("title") or "",
                "subtitle": record.get("subtitle") or "",
                "artist": record.get("artist") or "",
                "release_date": record.get("release_date") or "",
                "price": record.get("price") or "",
                "cover_url": cover_url,
                "source_url": record.get("source_url") or "",
                "tracks": json.loads(record.get("tracks_json") or "[]"),
                "specs": json.loads(record.get("spec_json") or "{}"),
                "extras": json.loads(record.get("extras_json") or "[]"),
                "detail_html": record.get("detail_html") or "",
            }
            pending_event_id = queue_site_event(
                self.config,
                source="music",
                item_id=release_id,
                change=(
                    "created"
                    if previous is None and not first_catalog_baseline
                    else "baseline"
                    if first_catalog_baseline or is_bot_baseline
                    else "updated"
                ),
                snapshot=snapshot,
                previous_id=str((previous or {}).get("updated_at") or ""),
                site_path=MUSIC_API_PATH,
                site_payload=site_payload,
            )
        response = await self.api_json(
            api,
            "POST",
            MUSIC_API_PATH,
            headers={
                **({"X-Nijidb-Poller-Event-ID": pending_event_id, "X-Nijidb-Poller": "1"} if pending_event_id else {}),
            },
            json=site_payload,
        )
        if (
            response.get("id") != release_id
            or type(response.get("changed")) is not bool
        ):
            raise SiteApiError("音乐导入返回的 ID 或状态无效")
        if pending_event_id:
            mark_site_event_imported(self.config, pending_event_id, changed=response["changed"])
            await flush_bot_event_outbox(self.config, api)
        if pending_event_id and not response["changed"] and not first_catalog_baseline:
            first_catalog_baseline = True
        self.state["stats"]["submitted"] += 1
        self.state["releases"][release_id] = {
            **image_state,
            "source_signature": signature,
            "bot_baseline_pending": False,
            "bot_baseline_seeded": True,
            "cover_source_url": source,
            "cover_url": cover_url,
            "checked_at": time.time(),
            "submitted_at": time.time(),
        }
        if first_catalog_baseline:
            return "seeded"
        return "changed" if response["changed"] else "skipped"

    async def run_pass(self, cycle_lock=None) -> dict[str, int]:
        result = dict.fromkeys(
            ("inspected", "changed", "seeded", "skipped", "errors"), 0
        )
        async with (
            official_news_client(self.config) as official,
            direct_api_client(self.config) as api,
        ):
            if not self.config.dry_run:
                status = await self.api_json(api, "GET", MUSIC_API_PATH)
                if status.get("version") != 1:
                    raise SiteApiError("音乐接口版本不支持")
                if self.config.bot_event_url:
                    await flush_site_event_outbox(self.config, api)
                    await flush_bot_event_outbox(self.config, api)
                else:
                    await self.api_json(api, "POST", MUSIC_API_PATH + "/notifications")
            existing = (
                await self.load_site_releases(api) if self.config.base_url else {}
            )
            entries, dynamic, failed = await self.get_catalog(official, cycle_lock)
            log(
                f"音乐目录：{len(entries)} 条，异步详情 {len(dynamic)} 条，失败 {len(failed)} 条"
            )
            if self.config.limit:
                entries = entries[: self.config.limit]
                selected = {entry[0] for entry in entries}
                failed.intersection_update(selected)
                dynamic = {key: value for key, value in dynamic.items() if key in selected}
            if not self.state["initialized"]:
                for release_id, _box, _image in entries:
                    self.state["releases"].setdefault(
                        release_id, {"baseline_pending": True}
                    )
                self.save()
            # Never delete baseline entries merely because one listing omits them.
            for release_id, box, image in entries[: self.config.limit or None]:
                result["inspected"] += 1
                if release_id in failed:
                    result["errors"] += 1
                    continue
                try:
                    record = parse_release(
                        release_id, dynamic.get(release_id) or box, image
                    )
                    outcome = await self.process_release(
                        official,
                        api,
                        record,
                        existing.get(release_id),
                        cycle_lock,
                    )
                    result[outcome] += 1
                    self.save()
                except (OfficialSiteBlocked, SiteApiError, PollerConfigError):
                    raise
                except (httpx.HTTPError, ValueError, BotoCoreError, ClientError) as exc:
                    result["errors"] += 1
                    log(
                        f"音乐处理失败：{release_id}（{type(exc).__name__}），保留上次断点"
                    )
                await asyncio.sleep(self.config.music_article_delay)
        self.state["initialized"] = True
        self.state["stats"]["passes"] += 1
        for key in ("seeded", "skipped", "errors"):
            self.state["stats"][key] += result[key]
        self.state["stats"].update(
            {f"last_{key}": value for key, value in result.items()}
        )
        self.state["last_pass_at"] = time.time()
        self.state["backoff_until"] = 0.0
        self.save()
        return result

    async def wait(self, stop_event: asyncio.Event, seconds: float, sleep_fn=asyncio.sleep) -> None:
        if sleep_fn is asyncio.sleep:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, seconds))
            except asyncio.TimeoutError:
                pass
        elif not stop_event.is_set():
            await sleep_fn(seconds)

    async def run(
        self,
        stop_event: asyncio.Event,
        cycle_lock: asyncio.Lock | None = None,
        state_lock=None,
        sleep_fn=asyncio.sleep,
    ) -> int:
        """Run after the parent acquires `state_lock`; the parent releases it."""
        loaded = False
        cycle_lock = cycle_lock or asyncio.Lock()
        lock = state_lock
        try:
            self.state = load_music_state(self.state_file)
            loaded = True
            log("音乐监控启动：共用新闻容器和固定代理，使用独立状态文件")
            while not stop_event.is_set():
                remaining = self.state["backoff_until"] - time.time()
                if remaining > 0:
                    if self.config.once:
                        return 1
                    await self.wait(stop_event, remaining, sleep_fn)
                    continue
                failed = False
                try:
                    result = await self.run_pass(cycle_lock)
                    failed = bool(result["errors"])
                    log(
                        f"音乐轮询完成：检查 {result['inspected']}，更新 {result['changed']}，基线 {result['seeded']}，错误 {result['errors']}"
                    )
                except OfficialSiteBlocked:
                    failed = True
                    self.state["backoff_until"] = (
                        time.time() + self.config.music_backoff_seconds
                    )
                    log(
                        f"官网音乐访问受限，退避 {self.config.music_backoff_seconds / 60:g} 分钟"
                    )
                except Exception as exc:
                    # Music monitoring is auxiliary: a defect here must never stop
                    # the news poller sharing this process.
                    failed = True
                    self.state["stats"]["errors"] += 1
                    log(f"音乐轮询失败：{type(exc).__name__}")
                    log(traceback.format_exc(limit=4).strip())
                finally:
                    self.save()
                    if self.config.bot_event_url:
                        try:
                            async with direct_api_client(self.config) as api:
                                await send_poller_heartbeat(
                                    self.config,
                                    api,
                                    {
                                        "source": "music",
                                        "running": not self.config.once and not stop_event.is_set(),
                                        "checked_at": time.time(),
                                        "last_pass_at": self.state.get("last_pass_at", 0),
                                        "backoff_until": self.state.get("backoff_until", 0),
                                        "stats": self.state.get("stats", {}),
                                    },
                                )
                        except (httpx.HTTPError, RuntimeError, ValueError) as heartbeat_exc:
                            log(f"音乐 Poller 心跳提交失败：{type(heartbeat_exc).__name__}")
                if self.config.once:
                    return int(failed)
                wait = (
                    min(self.config.rest_min_seconds, 300)
                    if failed
                    else random.uniform(
                        self.config.rest_min_seconds, self.config.rest_max_seconds
                    )
                )
                await self.wait(stop_event, wait, sleep_fn)
            return 0
        finally:
            if lock is not None and loaded:
                self.save()
            if self.s3 is not None:
                self.s3.close()
