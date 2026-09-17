#!/usr/bin/env python3
"""Sequentially archive Nijidb news images to R2 and import the R2 URLs.

The worker reads the public Nijidb news API, downloads only HTTPS image URLs
from the configured official hosts, uploads each image to R2, and then sends
that article back to ``POST /api/ingest/news``. It deliberately uses direct
HTTP connections, a fixed User-Agent, and no cookies. One optional fixed proxy
(``NEWS_WORKER_PROXY``) may be configured for hosts that cannot reach the
official site directly; there is never any proxy rotation.

Configuration is supplied through environment variables; see
``docs/remote-news-worker.md``. The API key is sent only in the import request
header and is never written to the state file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - reported when the script is run
    boto3 = None
    ClientError = Exception


DEFAULT_IMAGE_HOSTS = {
    "www.lovelive-anime.jp",
    "lovelive-anime.jp",
    "lovelive-as.bushimo.jp",
    "img.sunrise-inc.co.jp",
    "img.sunrise-inc.jp",
}
IMAGE_MAX_BYTES = 20 * 1024 * 1024
JSON_MAX_BYTES = 8 * 1024 * 1024
REDIRECT_CODES = {301, 302, 303, 307, 308}
# One fixed browser-style identity; no rotation, cookies, or proxy fallback.
WORKER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


class WorkerConfigError(RuntimeError):
    """The worker cannot start with the supplied environment."""


class WorkerRequestError(RuntimeError):
    """An HTTP request failed without exposing request URLs in logs."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class InvalidImageError(RuntimeError):
    """The response was successful but was not a supported raster image."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Return redirects to the caller so each target can be validated."""

    def redirect_request(self, request, file, code, message, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str
    api_key: str
    r2_endpoint: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_public_base_url: str
    r2_prefix: str
    state_file: Path
    delay_seconds: float
    work_seconds: float
    rest_seconds: float
    index_refresh_seconds: float
    request_timeout_seconds: float
    image_hosts: frozenset[str]
    once: bool
    dry_run: bool


def env_number(name: str, default: float, minimum: float = 0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WorkerConfigError(f"环境变量 {name} 必须是数字") from exc
    if not math.isfinite(value) or value < minimum:
        raise WorkerConfigError(f"环境变量 {name} 必须是不小于 {minimum:g} 的有限数字")
    return value


def split_hosts(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按顺序将 Nijidb 新闻图片归档到 R2")
    parser.add_argument("--once", action="store_true", help="只尝试一个图片，不进入工作/休息循环")
    parser.add_argument("--dry-run", action="store_true", help="只显示下一张待处理图片，不下载、不上传、不提交")
    parser.add_argument("--delay-seconds", type=float, default=None, help="图片之间的间隔，默认 30 秒，不能低于 30 秒")
    parser.add_argument("--work-minutes", type=float, default=None, help="工作窗口，默认 60 分钟")
    parser.add_argument("--rest-minutes", type=float, default=None, help="休息窗口，默认 30 分钟")
    parser.add_argument("--image-host", action="append", default=[], help="额外允许的图片 HTTPS 主机，可重复指定")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> WorkerConfig:
    base_url = os.getenv("NIJIDB_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("NIJIDB_INGEST_API_KEY", "").strip()
    endpoint = os.getenv("R2_ENDPOINT", "").strip().rstrip("/")
    bucket = os.getenv("R2_BUCKET", "nijidb").strip()
    access_key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    public_base = os.getenv("R2_PUBLIC_BASE_URL", "").strip().rstrip("/")
    prefix = os.getenv("R2_IMAGE_PREFIX", "images").strip("/")
    state_raw = os.getenv(
        "NEWS_WORKER_STATE_FILE",
        "~/.local/state/nijidb-news-worker/state.json",
    ).strip()

    missing = [
        name
        for name, value in (
            ("NIJIDB_BASE_URL", base_url),
            ("NIJIDB_INGEST_API_KEY", api_key),
            ("R2_ENDPOINT", endpoint),
            ("R2_BUCKET", bucket),
            ("R2_ACCESS_KEY_ID", access_key),
            ("R2_SECRET_ACCESS_KEY", secret_key),
            ("R2_PUBLIC_BASE_URL", public_base),
        )
        if not value
    ]
    if missing:
        raise WorkerConfigError(f"缺少环境变量：{', '.join(missing)}")
    if boto3 is None:
        raise WorkerConfigError("缺少 boto3，请先在当前 Python 环境安装 boto3")
    try:
        base = urlparse(base_url)
        public = urlparse(public_base)
        endpoint_url = urlparse(endpoint)
    except ValueError as exc:
        raise WorkerConfigError("网站或 R2 地址格式无效") from exc
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
        raise WorkerConfigError("NIJIDB_BASE_URL 必须是无凭据的 HTTP/HTTPS 地址")
    if public.scheme not in {"http", "https"} or not public.netloc or public.username or public.password:
        raise WorkerConfigError("R2_PUBLIC_BASE_URL 必须是无凭据的 HTTP/HTTPS 地址")
    if endpoint_url.scheme not in {"http", "https"} or not endpoint_url.netloc:
        raise WorkerConfigError("R2_ENDPOINT 必须是完整的 HTTP/HTTPS 地址")
    if public_base == endpoint.rstrip("/"):
        raise WorkerConfigError("R2_PUBLIC_BASE_URL 不能直接使用 R2 S3 API Endpoint")

    delay = args.delay_seconds if args.delay_seconds is not None else env_number("NEWS_WORKER_DELAY_SECONDS", 30, 30)
    if delay < 30:
        raise WorkerConfigError("图片间隔不能低于 30 秒")
    work_minutes = args.work_minutes if args.work_minutes is not None else env_number("NEWS_WORKER_WORK_MINUTES", 60, 1)
    rest_minutes = args.rest_minutes if args.rest_minutes is not None else env_number("NEWS_WORKER_REST_MINUTES", 30, 1)
    if not math.isfinite(work_minutes) or work_minutes < 1:
        raise WorkerConfigError("工作窗口必须是不小于 1 分钟的有限数字")
    if not math.isfinite(rest_minutes) or rest_minutes < 1:
        raise WorkerConfigError("休息窗口必须是不小于 1 分钟的有限数字")
    index_refresh = env_number("NEWS_WORKER_INDEX_REFRESH_SECONDS", 6 * 60 * 60, 60)
    timeout = env_number("NEWS_WORKER_REQUEST_TIMEOUT_SECONDS", 45, 5)
    hosts = set(DEFAULT_IMAGE_HOSTS)
    hosts.update(split_hosts(os.getenv("NEWS_WORKER_IMAGE_HOSTS", "")))
    hosts.update(str(item).strip().lower() for item in args.image_host if str(item).strip())
    if not hosts:
        raise WorkerConfigError("至少需要一个图片主机")
    return WorkerConfig(
        base_url=base_url,
        api_key=api_key,
        r2_endpoint=endpoint,
        r2_bucket=bucket,
        r2_access_key_id=access_key,
        r2_secret_access_key=secret_key,
        r2_public_base_url=public_base,
        r2_prefix=prefix,
        state_file=Path(os.path.expanduser(state_raw)),
        delay_seconds=delay,
        work_seconds=work_minutes * 60,
        rest_seconds=rest_minutes * 60,
        index_refresh_seconds=index_refresh,
        request_timeout_seconds=timeout,
        image_hosts=frozenset(hosts),
        once=args.once,
        dry_run=args.dry_run,
    )


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "article_ids": [],
        "index_page": 0,
        "page_count": 0,
        "article_index": 0,
        "image_index": 0,
        "last_index_refresh": 0.0,
        "pass_complete_at": 0.0,
        "next_pass_at": 0.0,
        "failures": {},
        "uploads": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerConfigError(f"无法读取状态文件：{path}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise WorkerConfigError(f"状态文件版本不支持：{path}")
    state = default_state()
    state.update(value)
    if not isinstance(state["article_ids"], list) or not isinstance(state["failures"], dict) or not isinstance(state["uploads"], dict):
        raise WorkerConfigError(f"状态文件格式无效：{path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def image_signature(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data.startswith(b"BM"):
        return ".bmp", "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return ".avif", "image/avif"
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"BM") and len(data) >= 26:
        return int.from_bytes(data[18:22], "little"), abs(int.from_bytes(data[22:26], "little", signed=True))
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if data[:2] == b"\xff\xd8":
        position = 2
        sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while position + 4 <= len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker == 0xDA:
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD9:
                continue
            length = int.from_bytes(data[position : position + 2], "big") if position + 2 <= len(data) else 0
            if length < 2 or position + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                height = int.from_bytes(data[position + 3 : position + 5], "big")
                width = int.from_bytes(data[position + 5 : position + 7], "big")
                return width, height
            position += length
    return None


def is_r2_public_url(value: Any, base_url: str, prefix: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        base = urlparse(base_url)
        candidate = urlparse(raw)
    except ValueError:
        return False
    if (
        candidate.scheme != base.scheme
        or candidate.netloc != base.netloc
        or candidate.username
        or candidate.password
        or candidate.query
        or candidate.fragment
    ):
        return False
    base_path = base.path.rstrip("/")
    candidate_path = candidate.path
    expected = f"{base_path}/{prefix}" if prefix else base_path
    if expected:
        expected = expected.rstrip("/")
        if not candidate_path.startswith(f"{expected}/"):
            return False
    elif not candidate_path.strip("/"):
        return False
    return all(part not in {".", ".."} for part in candidate_path.split("/") if part)


def source_key(article_id: str, source_url: str) -> str:
    return f"{article_id}:{source_url}"


def source_host(source_url: str) -> str:
    try:
        return (urlparse(source_url).hostname or "").lower()
    except ValueError:
        return ""


class NewsImageWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.state = load_state(config.state_file)
        self.stop_requested = False
        # 可选单条固定代理，用于官网直连被拒的运行环境；不做任何轮换。
        proxy = os.getenv("NEWS_WORKER_PROXY", "").strip()
        if proxy:
            parsed_proxy = urlparse(proxy)
            if parsed_proxy.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed_proxy.hostname:
                raise WorkerConfigError("NEWS_WORKER_PROXY 必须是完整的代理地址")
            handlers: dict[str, str] = {"http": proxy, "https": proxy}
        else:
            handlers = {}
        self.proxy_enabled = bool(proxy)
        self.opener = build_opener(ProxyHandler(handlers), NoRedirectHandler())
        self.base_parts = urlparse(config.base_url)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=config.r2_endpoint,
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name=os.getenv("R2_REGION", "auto"),
        )

    def log(self, message: str) -> None:
        print(f"[news-worker] {message}", flush=True)

    def request_bytes(
        self,
        url: str,
        headers: dict[str, str],
        max_bytes: int,
        allowed_hosts: set[str] | frozenset[str],
        require_https: bool,
    ) -> bytes:
        current = url
        for _ in range(5):
            try:
                parsed = urlparse(current)
            except ValueError as exc:
                raise WorkerRequestError("请求地址无效") from exc
            schemes = {"https"} if require_https else {"http", "https"}
            valid = (
                parsed.scheme in schemes
                and parsed.hostname in allowed_hosts
                and not parsed.username
                and not parsed.password
                and "\\" not in current
                and (parsed.scheme != "https" or parsed.port in {None, 443})
            )
            if not valid:
                raise WorkerRequestError("请求重定向到了不允许的地址")
            request = Request(current, headers=headers, method="GET")
            try:
                response = self.opener.open(request, timeout=self.config.request_timeout_seconds)
            except HTTPError as exc:
                if exc.code in REDIRECT_CODES:
                    location = exc.headers.get("Location", "")
                    if not location:
                        raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
                    current = urljoin(current, location)
                    continue
                raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
            except (TimeoutError, URLError, OSError) as exc:
                raise WorkerRequestError("网络请求失败") from exc
            with response:
                status = int(getattr(response, "status", 200))
                if status in REDIRECT_CODES:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise WorkerRequestError(f"HTTP {status}", status)
                    current = urljoin(current, location)
                    continue
                if status != 200:
                    raise WorkerRequestError(f"HTTP {status}", status)
                content_length = response.headers.get("Content-Length", "")
                try:
                    if content_length and int(content_length) > max_bytes:
                        raise WorkerRequestError("响应内容过大")
                except ValueError:
                    pass
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(64 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise WorkerRequestError("响应内容过大")
                return b"".join(chunks)
        raise WorkerRequestError("重定向次数过多")

    def api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.config.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self.request_bytes(
            self.api_url(path, query),
            {"Accept": "application/json", "User-Agent": WORKER_USER_AGENT},
            JSON_MAX_BYTES,
            {self.base_parts.hostname or ""},
            self.base_parts.scheme == "https",
        )
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerRequestError("网站 API 返回格式无效") from exc
        if not isinstance(value, dict):
            raise WorkerRequestError("网站 API 返回格式无效")
        return value

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > JSON_MAX_BYTES:
            raise WorkerRequestError("导入请求过大")
        request = Request(
            self.api_url(path),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": WORKER_USER_AGENT,
                "X-Nijidb-API-Key": self.config.api_key,
            },
            method="POST",
        )
        try:
            response = self.opener.open(request, timeout=self.config.request_timeout_seconds)
        except HTTPError as exc:
            raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise WorkerRequestError("网络请求失败") from exc
        with response:
            status = int(getattr(response, "status", 200))
            response_body = response.read(JSON_MAX_BYTES + 1)
        if len(response_body) > JSON_MAX_BYTES:
            raise WorkerRequestError("导入响应过大")
        if status < 200 or status >= 300:
            raise WorkerRequestError(f"HTTP {status}", status)
        try:
            value = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerRequestError("导入 API 返回格式无效") from exc
        if not isinstance(value, dict):
            raise WorkerRequestError("导入 API 返回格式无效")
        return value

    def load_listing_page(self, page: int) -> bool:
        data = self.get_json("/api/news", {"page": page, "page_size": 60})
        items = data.get("items", [])
        if not isinstance(items, list):
            raise WorkerRequestError("网站新闻列表格式无效")
        try:
            actual_page = max(1, int(data.get("page") or page))
            page_count = max(1, min(1000, int(data.get("pages") or 1)))
        except (TypeError, ValueError):
            actual_page = page
            page_count = 1
        ids: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            article_id = str(item.get("id") or "").strip()
            try:
                if "image_count" in item and int(item.get("image_count") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
            if article_id and article_id not in seen:
                ids.append(article_id)
                seen.add(article_id)
        self.state["article_ids"] = ids
        self.state["index_page"] = actual_page
        self.state["page_count"] = page_count
        self.state["article_index"] = 0
        self.state["image_index"] = 0
        save_state(self.config.state_file, self.state)
        self.log(f"读取新闻索引：第 {actual_page}/{page_count} 页，发现 {len(ids)} 篇含图片新闻")
        return bool(items)

    def refresh_index(self) -> None:
        self.load_listing_page(1)
        self.state["last_index_refresh"] = time.time()
        self.state["pass_complete_at"] = 0.0
        self.state["next_pass_at"] = 0.0
        save_state(self.config.state_file, self.state)

    def advance_page_or_pass(self) -> bool:
        current_page = int(self.state.get("index_page") or 0)
        page_count = int(self.state.get("page_count") or 1)
        if current_page >= page_count:
            self.complete_pass()
            return False
        return self.load_listing_page(current_page + 1)

    def prepare_pass(self) -> bool:
        now = time.time()
        if self.state.get("pass_complete_at"):
            if now < float(self.state.get("next_pass_at") or 0):
                return False
            self.refresh_index()
        elif not self.state.get("index_page"):
            self.refresh_index()
        while not self.state.get("article_ids"):
            if not self.advance_page_or_pass():
                return False
        return True

    def complete_article(self) -> None:
        self.state["article_index"] = int(self.state.get("article_index") or 0) + 1
        self.state["image_index"] = 0
        save_state(self.config.state_file, self.state)

    def complete_image(self) -> None:
        self.state["image_index"] = int(self.state.get("image_index") or 0) + 1
        save_state(self.config.state_file, self.state)

    def complete_pass(self) -> None:
        now = time.time()
        self.state["pass_complete_at"] = now
        self.state["next_pass_at"] = now + self.config.rest_seconds
        self.state["article_index"] = 0
        self.state["image_index"] = 0
        save_state(self.config.state_file, self.state)
        self.log("本轮索引处理完成，等待下一轮工作窗口")

    def article_detail(self, article_id: str) -> dict[str, Any]:
        data = self.get_json(f"/api/news/{quote(article_id, safe='')}")
        article = data.get("article")
        if not isinstance(article, dict):
            raise WorkerRequestError("新闻详情格式无效")
        return article

    def failure_record(self, key: str) -> dict[str, Any] | None:
        value = self.state.get("failures", {}).get(key)
        return value if isinstance(value, dict) else None

    def failure_is_due(self, key: str) -> bool:
        value = self.failure_record(key)
        if not value:
            return True
        try:
            return time.time() >= float(value.get("next_attempt_at") or 0)
        except (TypeError, ValueError):
            return True

    def record_failure(self, key: str, error: Exception, permanent: bool = False) -> None:
        previous = self.failure_record(key) or {}
        try:
            attempts = int(previous.get("attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        status = error.status if isinstance(error, WorkerRequestError) else None
        if permanent:
            cooldown = 365 * 24 * 60 * 60
        elif status == 403:
            cooldown = 6 * 60 * 60
        elif status == 429:
            cooldown = 60 * 60
        else:
            cooldown = min(6 * 60 * 60, 5 * 60 * (2 ** min(attempts - 1, 6)))
        self.state.setdefault("failures", {})[key] = {
            "attempts": attempts,
            "status": status,
            "next_attempt_at": time.time() + cooldown,
            "last_error": str(error)[:240],
        }
        save_state(self.config.state_file, self.state)

    def clear_failure(self, key: str) -> None:
        self.state.setdefault("failures", {}).pop(key, None)

    def image_payload(self, article: dict[str, Any]) -> list[dict[str, Any]]:
        raw_images = article.get("images", [])
        if not isinstance(raw_images, list):
            return []
        result: list[dict[str, Any]] = []
        uploads = self.state.setdefault("uploads", {})
        for raw in raw_images:
            if not isinstance(raw, dict) or str(raw.get("kind") or "remote") != "remote":
                continue
            source_url = str(raw.get("source_url") or "").strip()
            if not source_url:
                continue
            key = source_key(str(article.get("id") or ""), source_url)
            pending = uploads.get(key)
            current_public = str(raw.get("public_url") or "").strip()
            if not is_r2_public_url(current_public, self.config.r2_public_base_url, self.config.r2_prefix):
                current_public = str(raw.get("url") or "").strip()
            if not is_r2_public_url(current_public, self.config.r2_public_base_url, self.config.r2_prefix):
                current_public = ""
            item: dict[str, Any] = {
                "kind": "remote",
                "source_url": source_url,
                "public_url": current_public,
                "alt_text": str(raw.get("alt_text") or "").strip()[:500],
            }
            for field in ("width", "height", "bytes"):
                try:
                    value = int(raw.get(field) or 0)
                except (TypeError, ValueError):
                    value = 0
                item[field] = value if 0 <= value <= (100000 if field != "bytes" else IMAGE_MAX_BYTES) else 0
            sha256 = str(raw.get("sha256") or "").strip().lower()
            if len(sha256) == 64:
                item["sha256"] = sha256
            if isinstance(pending, dict):
                item.update(
                    {
                        "public_url": str(pending.get("public_url") or ""),
                        "bytes": int(pending.get("bytes") or item["bytes"]),
                        "sha256": str(pending.get("sha256") or item.get("sha256") or ""),
                    }
                )
                if pending.get("width"):
                    item["width"] = int(pending["width"])
                if pending.get("height"):
                    item["height"] = int(pending["height"])
            result.append(item)
        return result

    def article_import_payload(self, article: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(article.get("id") or "").strip(),
            "source": str(article.get("source") or "").strip(),
            "page_name": str(article.get("page_name") or "").strip(),
            "title": str(article.get("title") or "").strip(),
            "published_at": str(article.get("published_at") or "").strip(),
            "category": str(article.get("category") or "").strip(),
            "tags": article.get("tags") if isinstance(article.get("tags"), list) else [],
            "summary": str(article.get("summary") or "").strip(),
            "body_markdown": str(article.get("body_markdown") or ""),
            "source_url": str(article.get("source_url") or "").strip(),
            "source_hash": str(article.get("source_hash") or "").strip(),
            "images": self.image_payload(article),
        }
        return payload

    def archive_image(self, data: bytes) -> dict[str, Any]:
        signature = image_signature(data)
        if not signature:
            raise InvalidImageError("响应不是支持的图片格式")
        extension, content_type = signature
        digest = hashlib.sha256(data).hexdigest()
        key = f"{self.config.r2_prefix + '/' if self.config.r2_prefix else ''}news-remote/{digest}{extension}"
        exists = False
        try:
            response = self.s3.head_object(Bucket=self.config.r2_bucket, Key=key)
            exists = int(response.get("ContentLength") or 0) == len(data)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "403", "NoSuchKey", "NotFound"}:
                raise RuntimeError("R2 对象检查失败") from exc
            if code == "403":
                raise RuntimeError("R2 对象检查被拒绝") from exc
        if not exists:
            try:
                self.s3.put_object(
                    Bucket=self.config.r2_bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    CacheControl="public, max-age=31536000, immutable",
                    Metadata={"sha256": digest},
                )
            except Exception as exc:
                raise RuntimeError("R2 图片上传失败") from exc
        dimensions = image_dimensions(data)
        public_url = f"{self.config.r2_public_base_url}/{quote(key, safe='/')}"
        return {
            "public_url": public_url,
            "sha256": digest,
            "bytes": len(data),
            "width": dimensions[0] if dimensions else 0,
            "height": dimensions[1] if dimensions else 0,
        }

    def fetch_image(self, source_url: str, article_source_url: str) -> tuple[bytes, str]:
        host = source_host(source_url)
        parsed = urlparse(source_url)
        if parsed.scheme != "https":
            raise InvalidImageError("图片来源不是 HTTPS")
        if not host or host not in self.config.image_hosts:
            raise InvalidImageError("图片主机不在允许列表")
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja-JP;q=0.8,ja;q=0.7",
            "Referer": article_source_url,
            "User-Agent": WORKER_USER_AGENT,
        }
        return self.request_bytes(source_url, headers, IMAGE_MAX_BYTES, self.config.image_hosts, True), host

    def step(self) -> bool:
        if not self.prepare_pass():
            return False
        article_ids = self.state.get("article_ids", [])
        article_index = int(self.state.get("article_index") or 0)
        if article_index >= len(article_ids):
            self.advance_page_or_pass()
            return False
        article_id = str(article_ids[article_index])
        try:
            article = self.article_detail(article_id)
        except WorkerRequestError as exc:
            if exc.status == 404:
                self.log(f"跳过已不存在的新闻：{article_id}")
                self.complete_article()
                return False
            raise
        raw_images = article.get("images", [])
        images = [
            image
            for image in raw_images
            if isinstance(image, dict) and str(image.get("kind") or "remote") == "remote" and str(image.get("source_url") or "").strip()
        ] if isinstance(raw_images, list) else []
        image_index = int(self.state.get("image_index") or 0)
        if image_index >= len(images):
            self.complete_article()
            return False
        image = images[image_index]
        source_url = str(image.get("source_url") or "").strip()
        key = source_key(article_id, source_url)
        current_public = str(image.get("public_url") or "").strip()
        if not is_r2_public_url(current_public, self.config.r2_public_base_url, self.config.r2_prefix):
            current_public = str(image.get("url") or "").strip()
        if is_r2_public_url(current_public, self.config.r2_public_base_url, self.config.r2_prefix):
            self.clear_failure(key)
            self.state.setdefault("uploads", {}).pop(key, None)
            self.complete_image()
            return False
        failure = self.failure_record(key)
        if failure and not self.failure_is_due(key):
            self.complete_image()
            return False
        pending = self.state.setdefault("uploads", {}).get(key)
        if not isinstance(pending, dict):
            if self.config.dry_run:
                self.log(f"下一张图片：新闻 {article_id}，图片 {image.get('id', image_index)}，主机 {source_host(source_url)}")
                return False
            host = source_host(source_url)
            try:
                data, host = self.fetch_image(source_url, str(article.get("source_url") or ""))
                uploaded = self.archive_image(data)
            except InvalidImageError as exc:
                self.record_failure(key, exc, permanent=True)
                self.log(f"跳过图片：新闻 {article_id}，原因：{exc}")
                self.complete_image()
                return False
            except WorkerRequestError as exc:
                self.record_failure(key, exc)
                self.log(f"图片请求失败：新闻 {article_id}，主机 {host}，状态 {exc.status or 'network'}")
                self.complete_image()
                return True
            except RuntimeError as exc:
                self.record_failure(key, exc)
                self.log(f"图片归档失败：新闻 {article_id}，原因：{exc}")
                return True
            pending = uploaded
            self.state.setdefault("uploads", {})[key] = pending
            save_state(self.config.state_file, self.state)
            self.log(f"图片已上传 R2，等待提交：新闻 {article_id}，主机 {host}")
        payload = self.article_import_payload(article)
        try:
            self.post_json("/api/ingest/news", payload)
        except WorkerRequestError as exc:
            if exc.status in {400, 413, 415, 422}:
                self.record_failure(key, exc, permanent=True)
                self.log(f"导入内容被拒绝：新闻 {article_id}，状态 {exc.status}")
                self.complete_image()
                return False
            raise
        self.state.setdefault("uploads", {}).pop(key, None)
        self.clear_failure(key)
        self.complete_image()
        self.log(f"已提交 R2 图片地址：新闻 {article_id}，图片 {image.get('id', image_index)}")
        return True

    def sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0, seconds)
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5))

    def run(self) -> None:
        if self.config.once:
            self.step()
            return
        while not self.stop_requested:
            work_deadline = time.monotonic() + self.config.work_seconds
            while not self.stop_requested and time.monotonic() < work_deadline:
                started = time.monotonic()
                try:
                    progressed = self.step()
                except WorkerRequestError as exc:
                    wait = 10 * 60 if exc.status in {401, 403, 429, 503} else 60
                    self.log(f"服务请求失败，{wait // 60 if wait >= 60 else wait} 分钟后重试：状态 {exc.status or 'network'}")
                    self.sleep_interruptibly(min(wait, max(1, work_deadline - time.monotonic())))
                    continue
                except (RuntimeError, OSError) as exc:
                    self.log(f"工作步骤失败，60 秒后重试：{str(exc)[:240]}")
                    self.sleep_interruptibly(min(60, max(1, work_deadline - time.monotonic())))
                    continue
                elapsed = time.monotonic() - started
                if progressed:
                    self.sleep_interruptibly(max(0, self.config.delay_seconds - elapsed))
                else:
                    self.sleep_interruptibly(min(60, max(1, work_deadline - time.monotonic())))
            if self.stop_requested:
                break
            self.log(f"工作窗口结束，休息 {self.config.rest_seconds / 60:g} 分钟")
            self.sleep_interruptibly(self.config.rest_seconds)
        save_state(self.config.state_file, self.state)


def main() -> int:
    args = parse_args()
    try:
        config = build_config(args)
        worker = NewsImageWorker(config)
    except WorkerConfigError as exc:
        print(f"[news-worker] 配置错误：{exc}", file=sys.stderr)
        return 2

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        worker.stop_requested = True
        worker.log(f"收到停止信号 {signum}，将在当前请求后退出")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    worker.log(f"启动：站点 {config.base_url}，出口 {'固定代理' if worker.proxy_enabled else '直连'}，图片间隔 {config.delay_seconds:g}s")
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop_requested = True
        save_state(config.state_file, worker.state)
    except (WorkerRequestError, RuntimeError, OSError) as exc:
        print(f"[news-worker] 运行失败：{str(exc)[:240]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
