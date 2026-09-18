#!/usr/bin/env python3
"""官网新闻轮询：抓列表 → 解析详情 → 图片上传 R2 → 提交到 /api/ingest/news。

用途：当站点服务器所在 IP 被官网 CloudFront 拦截（HTTP 403）而无法自行轮询时，
把官网内容的检测与更新放到另一台能直连官网的机器上运行。

设计要点：

* **官网请求走独立固定代理**（``NEWS_POLL_PROXY``），与运行机器自身的代理/路由器
  配置互不影响，也不会打乱对直连链路的 403 观测。
* **站点 API 直连**（不使用代理），避免无谓绕路。
* 图片先上传 R2，再把 R2 公开地址与文章一起提交；提交接口本身是幂等的。
* 串行、低频、遇到 403 进入退避；不做 UA 轮换、Cookie、并发抓取。
* 官网详情页 HTML 带时间相关动态内容且不返回 ETag，所以变更判断用解析后的内容指纹，
  不使用原文哈希。

示例：
    NIJIDB_BASE_URL=https://example.com NIJIDB_INGEST_API_KEY=... \
    NEWS_POLL_PROXY=http://127.0.0.1:<port> R2_ENDPOINT=... R2_BUCKET=... \
    R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_PUBLIC_BASE_URL=... \
    uv run --locked python scripts/local_official_news_poller.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import random
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.news import (  # noqa: E402
    NEWS_TOPICS_URL,
    parse_topic_detail,
    parse_topic_listing,
    topic_next_offset,
)
from logfmt import format_message  # noqa: E402
from mihomo_config import ConfigError as MihomoConfigError, validate_proxy_host  # noqa: E402
from app.news_fetch import (  # noqa: E402
    NEWS_HEADERS,
    fetch_news_page,
    filter_news_images,
    image_dimensions_from_bytes,
    is_allowed_official_image_url,
    is_avif_bytes,
)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None
    ClientError = Exception


IMAGE_MAX_BYTES = 20 * 1024 * 1024
API_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
INGEST_PATH = "/api/ingest/news"
STATE_VERSION = 1
STATE_MAX_BYTES = 4 * 1024 * 1024
MAX_POLL_PAGES = 20
OK_NODE_STATUSES = {200, 206}


class PollerConfigError(RuntimeError):
    """配置不完整或非法。"""


class OfficialSiteBlocked(RuntimeError):
    """官网返回 403，本次通行中止。"""


class SiteApiError(RuntimeError):
    """站点 API 不可用，本次通行中止且不切换官网出口。"""


@dataclass
class PollerConfig:
    base_url: str
    api_key: str
    proxy: str
    r2_endpoint: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_public_base_url: str
    r2_prefix: str
    state_file: Path
    article_delay: float
    image_delay: float
    rest_min_seconds: float
    rest_max_seconds: float
    max_pages: int
    timeout: float
    backoff_seconds: float
    nodes_file: Path | None = None
    hints_file: Path | None = None
    config_file: Path | None = None
    proxy_host: str = "mihomo"
    dry_run: bool = False
    once: bool = False
    limit: int = 0
    refresh_existing: bool = False
    only: list[str] = field(default_factory=list)


def env_float(name: str, default: float, minimum: float = 0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PollerConfigError(f"环境变量 {name} 必须是数字") from exc
    if not math.isfinite(value) or value < minimum:
        raise PollerConfigError(f"环境变量 {name} 必须是不小于 {minimum:g} 的有限数字")
    return value


def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PollerConfigError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum or maximum is not None and value > maximum:
        bound = f"{minimum}–{maximum}" if maximum is not None else f"不小于 {minimum}"
        raise PollerConfigError(f"环境变量 {name} 必须在 {bound} 范围内")
    return value


def default_state_path() -> Path:
    return Path.home() / ".local/state/nijidb-news-poller/state.json"


def validate_http_url(value: str, field: str, *, allow_path: bool = True) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
        decoded_path = unquote(parsed.path)
        path_parts = decoded_path.split("/")
    except (TypeError, ValueError) as exc:
        raise PollerConfigError(f"{field} 必须是无凭据的 HTTP/HTTPS 地址") from exc
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        or "?" in decoded_path
        or "#" in decoded_path
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any(character.isspace() for character in parsed.hostname)
        or port is not None and not 1 <= port <= 65535
        or parsed.netloc.endswith(":")
        or any(part in {".", ".."} for part in path_parts if part)
        or any(not part for part in path_parts[1:-1])
        or parsed.username
        or parsed.password
        or parsed.query
        or "?" in value
        or parsed.fragment
        or "#" in value
        or not allow_path and parsed.path not in {"", "/"}
        or "\\" in value
        or "\\" in decoded_path
    ):
        raise PollerConfigError(f"{field} 必须是无凭据的 HTTP/HTTPS 地址")
    return value


def validate_proxy_url(value: str) -> str:
    return validate_http_url(value, "NEWS_POLL_PROXY", allow_path=False)


def effective_http_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def same_http_origin(first: str, second: str) -> bool:
    try:
        first_parsed = urlparse(first)
        second_parsed = urlparse(second)
        first_port = effective_http_port(first_parsed)
        second_port = effective_http_port(second_parsed)
    except (TypeError, ValueError):
        return False
    return (
        first_parsed.scheme == second_parsed.scheme
        and (first_parsed.hostname or "").lower() == (second_parsed.hostname or "").lower()
        and first_port == second_port
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="官网新闻轮询（经固定代理，图片上传 R2）")
    parser.add_argument("--once", action="store_true", help="只跑一趟就退出，不进入休息循环")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和解析，不下载图片、不上传、不提交")
    parser.add_argument("--limit", type=int, default=0, help="本次最多检查多少篇（0 表示不限）")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="忽略跳过条件，对站点已有的文章也重新提交（对账用）",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PAGE_NAME",
        help="只处理指定 page_name，可重复指定",
    )
    parser.add_argument("--pages", type=int, default=0, help="最多翻多少页列表（0 表示用环境变量默认值）")
    parser.add_argument("--delay-seconds", type=float, default=None, help="文章之间的间隔，默认 30 秒")
    parser.add_argument("--image-delay-seconds", type=float, default=None, help="图片之间的间隔，默认 10 秒")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> PollerConfig:
    base_url = os.getenv("NIJIDB_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("NIJIDB_INGEST_API_KEY", "").strip()
    proxy = os.getenv("NEWS_POLL_PROXY", "").strip()
    if proxy:
        proxy = validate_proxy_url(proxy)
    endpoint = os.getenv("R2_ENDPOINT", "").strip().rstrip("/")
    bucket = os.getenv("R2_BUCKET", "nijidb").strip()
    access_key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    public_base = os.getenv("R2_PUBLIC_BASE_URL", "").strip().rstrip("/")
    prefix = os.getenv("R2_IMAGE_PREFIX", "images").strip("/")
    if (
        len(prefix) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in prefix)
        or "\\" in prefix
        or prefix and any(part in {"", ".", ".."} for part in prefix.split("/"))
    ):
        raise PollerConfigError("R2_IMAGE_PREFIX 格式无效")
    state_raw = os.getenv("NEWS_POLL_STATE_FILE", "").strip()
    config_file_raw = os.getenv("NEWS_POLL_CONFIG_FILE", "/config/config.yaml").strip()

    missing = [
        name
        for name, value in (
            ("NIJIDB_BASE_URL", base_url),
            ("NIJIDB_INGEST_API_KEY", api_key),
            ("NEWS_POLL_PROXY", proxy),
            ("R2_ENDPOINT", endpoint),
            ("R2_BUCKET", bucket),
            ("R2_ACCESS_KEY_ID", access_key),
            ("R2_SECRET_ACCESS_KEY", secret_key),
            ("R2_PUBLIC_BASE_URL", public_base),
        )
        if not value
    ]
    if missing and not args.dry_run:
        raise PollerConfigError(f"缺少环境变量：{', '.join(missing)}")
    if base_url:
        validate_http_url(base_url, "NIJIDB_BASE_URL")
    if endpoint:
        validate_http_url(endpoint, "R2_ENDPOINT")
    if public_base:
        validate_http_url(public_base, "R2_PUBLIC_BASE_URL")
    if endpoint and same_http_origin(public_base, endpoint):
        raise PollerConfigError("R2_PUBLIC_BASE_URL 不能直接使用 R2 S3 API Endpoint")
    if len(api_key) > 256:
        raise PollerConfigError("NIJIDB_INGEST_API_KEY 不能超过 256 个字符")
    if boto3 is None and not args.dry_run:
        raise PollerConfigError("缺少 boto3")

    article_delay = args.delay_seconds if args.delay_seconds is not None else env_float("NEWS_POLL_ARTICLE_DELAY_SECONDS", 30, 5)
    image_delay = args.image_delay_seconds if args.image_delay_seconds is not None else env_float("NEWS_POLL_IMAGE_DELAY_SECONDS", 10, 1)
    if not math.isfinite(article_delay) or article_delay < 5:
        raise PollerConfigError("文章间隔必须是不小于 5 秒的有限数字")
    if not math.isfinite(image_delay) or image_delay < 1:
        raise PollerConfigError("图片间隔必须是不小于 1 秒的有限数字")
    if args.pages < 0 or args.pages > MAX_POLL_PAGES:
        raise PollerConfigError(f"--pages 必须在 0–{MAX_POLL_PAGES} 范围内")
    if args.limit < 0 or args.limit > 10000:
        raise PollerConfigError("--limit 必须在 0–10000 范围内")
    pages = args.pages or env_int("NEWS_POLL_MAX_PAGES", 1, 1, MAX_POLL_PAGES)
    # 两趟之间的间隔取随机值，避免固定节奏：默认 30～60 分钟。
    default_rest = env_float("NEWS_POLL_REST_MINUTES", 30, 1)
    rest_min = env_float("NEWS_POLL_REST_MIN_MINUTES", default_rest, 1) * 60
    rest_max = env_float("NEWS_POLL_REST_MAX_MINUTES", default_rest, 1) * 60
    if rest_max < rest_min:
        raise PollerConfigError("NEWS_POLL_REST_MAX_MINUTES 不能小于 NEWS_POLL_REST_MIN_MINUTES")
    proxy_host = os.getenv("NEWS_POLL_PROXY_HOST", "mihomo").strip() or "mihomo"
    try:
        proxy_host = validate_proxy_host(proxy_host, "NEWS_POLL_PROXY_HOST")
    except MihomoConfigError as exc:
        raise PollerConfigError(str(exc)) from exc
    return PollerConfig(
        base_url=base_url,
        api_key=api_key,
        proxy=proxy,
        r2_endpoint=endpoint,
        r2_bucket=bucket,
        r2_access_key_id=access_key,
        r2_secret_access_key=secret_key,
        r2_public_base_url=public_base,
        r2_prefix=prefix,
        state_file=Path(state_raw).expanduser() if state_raw else default_state_path(),
        article_delay=article_delay,
        image_delay=image_delay,
        rest_min_seconds=rest_min,
        rest_max_seconds=rest_max,
        max_pages=pages,
        timeout=env_float("NEWS_POLL_TIMEOUT_SECONDS", 45, 5),
        backoff_seconds=env_float("NEWS_POLL_BACKOFF_MINUTES", 30, 1) * 60,
        nodes_file=(
            Path(os.environ["NEWS_POLL_NODES_FILE"]).expanduser()
            if os.getenv("NEWS_POLL_NODES_FILE", "").strip()
            else None
        ),
        hints_file=(
            Path(os.environ["NEWS_POLL_HINTS_FILE"]).expanduser()
            if os.getenv("NEWS_POLL_HINTS_FILE", "").strip()
            else None
        ),
        config_file=Path(config_file_raw or "/config/config.yaml").expanduser(),
        proxy_host=proxy_host,
        dry_run=args.dry_run,
        once=args.once,
        limit=args.limit,
        refresh_existing=bool(args.refresh_existing),
        only=[str(name).strip() for name in args.only if str(name).strip()],
    )


def log(message: str) -> None:
    print(format_message(message), flush=True)


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "articles": {},
        "stats": {"passes": 0, "pushed": 0, "seeded": 0, "skipped_unchanged": 0, "skipped_existing": 0, "errors": 0},
        "backoff_until": 0.0,
        "last_pass_at": 0.0,
        "node_index": 0,
    }


def _read_state_bytes(path: Path) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(STATE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PollerConfigError(f"状态文件不能是符号链接：{path}")
    if not path.exists():
        return default_state()
    try:
        raw = _read_state_bytes(path)
        if raw is None:
            raise OSError("状态文件不是普通文件")
        if len(raw) > STATE_MAX_BYTES:
            raise PollerConfigError(f"状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB 限制：{path}")
        value = json.loads(raw.decode("utf-8"))
    except PollerConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PollerConfigError(f"无法读取状态文件：{path}") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != STATE_VERSION:
        raise PollerConfigError(f"状态文件版本不支持：{path}")
    state = default_state()
    state.update(value)
    if not isinstance(state.get("articles"), dict) or not isinstance(state.get("stats"), dict):
        raise PollerConfigError(f"状态文件格式无效：{path}")
    state["articles"] = {
        str(page_name).strip(): item
        for page_name, item in state["articles"].items()
        if 0 < len(str(page_name).strip()) <= 128 and isinstance(item, dict)
    }
    merged_stats = default_state()["stats"]
    merged_stats.update(
        {
            key: value
            for key, value in state["stats"].items()
            if key in merged_stats and type(value) is int and 0 <= value <= 2**63 - 1
        }
    )
    state["stats"] = merged_stats
    for key in ("backoff_until", "last_pass_at"):
        raw_number = state.get(key, 0.0)
        if isinstance(raw_number, bool):
            number = 0.0
        else:
            try:
                number = float(raw_number)
            except (TypeError, ValueError, OverflowError):
                number = 0.0
        state[key] = number if math.isfinite(number) and number >= 0 else 0.0
    node_index = state.get("node_index", 0)
    state["node_index"] = node_index if type(node_index) is int and node_index >= 0 else 0
    if state.get("last_node") is not None:
        last_node = str(state["last_node"])
        state["last_node"] = last_node[:256]
    return state


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = _read_state_bytes(path)
        if raw is None:
            return {}
        if len(raw) > STATE_MAX_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_text(path: Path, text: str) -> None:
    if path.parent.is_symlink():
        raise PollerConfigError("状态文件目录不能是符号链接")
    if len(text.encode("utf-8")) > STATE_MAX_BYTES:
        raise PollerConfigError(f"状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB 限制：{path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def save_state(path: Path, state: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise PollerConfigError("状态文件目录不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def acquire_state_lock(state_path: Path):
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    if lock_path.parent.is_symlink():
        raise PollerConfigError("状态文件目录不能是符号链接")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock_path.parent, 0o700)
    except OSError:
        pass
    handle = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if handle is not None:
            handle.close()
        raise PollerConfigError("已有相同状态文件的新闻轮询正在运行") from exc
    return handle


def news_listing_url(offset: int) -> str:
    separator = "&" if "?" in NEWS_TOPICS_URL else "?"
    return f"{NEWS_TOPICS_URL}{separator}offset={offset}"


def image_signature(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpeg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data.startswith(b"BM"):
        return ".bmp", "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if is_avif_bytes(data):
        return ".avif", "image/avif"
    return None


def content_signature(record: dict[str, Any]) -> str:
    """文章变更判断用的稳定指纹。

    官网 HTML 带有时间相关动态内容（同一篇每次抓取的哈希都不同），因此不能用
    原文哈希判断变更，只能用解析后的稳定字段。
    """
    payload = {
        "title": record.get("title") or "",
        "published_at": record.get("published_at") or "",
        "category": record.get("category") or "",
        "summary": record.get("summary") or "",
        "body": record.get("body_markdown") or "",
        "source_url": record.get("source_url") or "",
        "images": [
            str(image.get("source_url") or "")
            for image in (record.get("images") or [])
            if isinstance(image, dict)
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def image_source_set(images: list[dict[str, Any]] | Any) -> set[str]:
    if not isinstance(images, list):
        return set()
    return {
        str(image.get("source_url") or "").strip()
        for image in images
        if isinstance(image, dict) and str(image.get("source_url") or "").strip()
    }


def archived_image_source_set(images: list[dict[str, Any]] | Any) -> set[str]:
    if not isinstance(images, list):
        return set()
    return {
        str(image.get("source_url") or "").strip()
        for image in images
        if (
            isinstance(image, dict)
            and str(image.get("source_url") or "").strip()
            and str(image.get("public_url") or "").strip()
        )
    }


async def read_response_bytes(response: httpx.Response, maximum: int) -> bytes:
    content_length = response.headers.get("content-length", "")
    try:
        if content_length and int(content_length) > maximum:
            raise SiteApiError("站点 API 响应过大")
    except ValueError:
        pass
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        remaining = maximum - len(body)
        if len(chunk) > remaining:
            raise SiteApiError("站点 API 响应过大")
        body.extend(chunk)
    return bytes(body)


class OfficialNewsPoller:
    """串行轮询官网新闻并把结果提交到站点接口。"""

    def __init__(self, config: PollerConfig):
        self.config = config
        self.state = load_state(config.state_file)
        self.production: dict[str, dict[str, Any]] = {}
        self.current_node = ""
        self.retry_count = 0
        self.s3 = None
        if not config.dry_run:
            self.s3 = boto3.client(
                "s3",
                endpoint_url=config.r2_endpoint,
                aws_access_key_id=config.r2_access_key_id,
                aws_secret_access_key=config.r2_secret_access_key,
                region_name="auto",
            )

    # ---------- 出口节点 ----------

    def read_nodes(self) -> list[dict[str, Any]]:
        """读取健康检查产出的可用节点列表（含每个节点的独立入口端口）。"""
        if not self.config.nodes_file or not self.config.nodes_file.exists():
            return []
        try:
            with self.config.nodes_file.open("rb") as handle:
                raw = handle.read(STATE_MAX_BYTES + 1)
            if len(raw) > STATE_MAX_BYTES:
                raise ValueError("节点状态文件过大")
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            log(f"读取节点列表失败：{type(exc).__name__}")
            return []
        if self.config.config_file:
            if not self.config.config_file.is_file():
                log("无法读取 mihomo 配置，忽略健康名单")
                return []
            expected_digest = str(payload.get("config_sha256") or "") if isinstance(payload, dict) else ""
            if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                log("节点列表缺少有效的配置摘要，忽略健康名单")
                return []
            try:
                actual_digest = hashlib.sha256(self.config.config_file.read_bytes()).hexdigest()
            except OSError:
                log("无法读取 mihomo 配置，忽略健康名单")
                return []
            if expected_digest != actual_digest:
                log("节点列表与当前 mihomo 配置不匹配，忽略健康名单")
                return []
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        valid_nodes: list[dict[str, Any]] = []
        for item in nodes or []:
            if not isinstance(item, dict) or not isinstance(item.get("node"), str) or not item["node"].strip():
                continue
            if type(item.get("port")) is not int or not 1 <= item["port"] <= 65535:
                continue
            valid_nodes.append({**item, "port": item["port"], "node": item["node"].strip()})
        return valid_nodes

    def node_has_recent_hint(self, name: str, checked_at: str) -> bool:
        """本进程刚在这条节点上失败过，且失败晚于最近一次成功探测。"""
        if not self.config.hints_file or not self.config.hints_file.exists():
            return False
        payload = _read_json_file(self.config.hints_file)
        try:
            hints = payload.get("nodes") if isinstance(payload, dict) else {}
            raw_failed_at = hints.get(name) if isinstance(hints, dict) else 0
            failed_at = float(raw_failed_at or 0)
        except (OSError, ValueError, TypeError, OverflowError, json.JSONDecodeError):
            return False
        if not math.isfinite(failed_at) or failed_at <= 0:
            return False
        try:
            checked = datetime.fromisoformat(checked_at).timestamp()
        except (OSError, TypeError, ValueError, OverflowError):
            checked = 0.0
        return math.isfinite(checked) and failed_at > checked

    def record_hint(self, name: str) -> None:
        """记下失败节点，供健康检查优先重测、也供本进程避开。"""
        if not self.config.hints_file or not name:
            return
        hints = _read_json_file(self.config.hints_file)
        nodes = hints.get("nodes") if isinstance(hints.get("nodes"), dict) else {}
        nodes[name] = time.time()
        # 只保留最近一天的记录，避免文件无限增长
        cutoff = time.time() - 86400
        hints["nodes"] = {
            str(key): value
            for key, value in nodes.items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > cutoff
        }
        self.config.hints_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config.hints_file.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(self.config.hints_file, json.dumps(hints, ensure_ascii=False, indent=2) + "\n")
        try:
            os.chmod(self.config.hints_file, 0o600)
        except OSError:
            pass

    def choose_proxy(self) -> tuple[str, str]:
        """每趟开始时选一个健康节点；没有可用列表时退回固定代理。"""
        candidates = [
            item
            for item in self.read_nodes()
            if item.get("status") in OK_NODE_STATUSES and not self.node_has_recent_hint(str(item.get("node") or ""), str(item.get("checked_at") or ""))
        ]
        if not candidates:
            return self.config.proxy, "固定代理"
        last = str(self.state.get("last_node") or "")
        pool = [item for item in candidates if str(item.get("node")) != last] or candidates
        chosen = random.choice(pool)
        name = str(chosen.get("node") or chosen.get("name") or "未知")
        self.state["last_node"] = name
        return f"http://{self.config.proxy_host}:{int(chosen['port'])}", name

    # ---------- 图片 ----------

    async def fetch_image_bytes(self, client: httpx.AsyncClient, url: str, referer: str) -> bytes:
        if not is_allowed_official_image_url(url):
            raise ValueError("图片来源不在允许的官网页内")
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja-JP;q=0.8,ja;q=0.7",
            "Referer": referer,
        }
        async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                raise ValueError("图片发生跳转，已跳过")
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                remaining = IMAGE_MAX_BYTES - len(content)
                if len(chunk) > remaining:
                    raise ValueError("图片超过 20 MB 限制")
                content.extend(chunk)
        return bytes(content)

    def archive_image(self, data: bytes) -> dict[str, Any]:
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError("图片超过 20 MB 限制")
        signature = image_signature(data)
        dimensions = image_dimensions_from_bytes(data)
        if not signature or not dimensions:
            raise ValueError("响应不是有效的支持图片")
        if dimensions[0] < 240 or dimensions[1] < 120 or dimensions[0] > 10000 or dimensions[1] > 10000:
            raise ValueError("图片尺寸不在允许范围内")
        extension, content_type = signature
        digest = hashlib.sha256(data).hexdigest()
        prefix = f"{self.config.r2_prefix}/" if self.config.r2_prefix else ""
        key = f"{prefix}news-remote/{digest}{extension}"
        exists = False
        try:
            head = self.s3.head_object(Bucket=self.config.r2_bucket, Key=key)
            exists = int(head.get("ContentLength") or 0) == len(data)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code == "403":
                raise RuntimeError("R2 对象检查被拒绝") from exc
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise RuntimeError("R2 对象检查失败") from exc
        except Exception as exc:
            raise RuntimeError("R2 对象检查失败") from exc
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
        return {
            "public_url": f"{self.config.r2_public_base_url}/{quote(key, safe='/')}",
            "sha256": digest,
            "bytes": len(data),
            "width": dimensions[0],
            "height": dimensions[1],
        }

    # ---------- 站点侧比对 ----------

    async def load_production_index(self, api: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        """读取站点最新新闻（首页 100 条），用于判断哪些需要提交。"""
        async with api.stream(
            "GET",
            f"{self.config.base_url}/api/news",
            params={"page": 1, "page_size": 100},
        ) as response:
            response.raise_for_status()
            body = await read_response_bytes(response, API_MAX_RESPONSE_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SiteApiError("站点新闻索引格式无效") from exc
        if not isinstance(payload, dict):
            raise SiteApiError("站点新闻索引格式无效")
        index: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            page_name = str(item.get("page_name") or "")
            if page_name:
                try:
                    image_count = max(0, int(item.get("image_count") or 0))
                except (TypeError, ValueError, OverflowError):
                    image_count = 0
                r2_image_count: int | None = None
                if "r2_image_count" in item:
                    try:
                        r2_image_count = max(0, int(item.get("r2_image_count") or 0))
                    except (TypeError, ValueError, OverflowError):
                        r2_image_count = 0
                index[page_name] = {
                    "id": str(item.get("id") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                    "cover_url": str(item.get("cover_url") or ""),
                    "image_count": image_count,
                    "r2_image_count": r2_image_count,
                }
        return index

    # ---------- 提交 ----------

    async def submit(self, client: httpx.AsyncClient, article: dict[str, Any]) -> None:
        payload = {"article": {key: value for key, value in article.items() if value is not None}}
        async with client.stream(
            "POST",
            f"{self.config.base_url}{INGEST_PATH}",
            json=payload,
            headers={"X-Nijidb-API-Key": self.config.api_key},
        ) as response:
            if response.status_code == 200:
                return
            body = await read_response_bytes(response, 16 * 1024)
            detail = ""
            try:
                error_payload = json.loads(body.decode("utf-8"))
                if isinstance(error_payload, dict):
                    detail = str(error_payload.get("detail") or "")
            except (UnicodeDecodeError, ValueError):
                detail = body[:200].decode("utf-8", errors="replace")
            raise SiteApiError(f"提交失败 HTTP {response.status_code}：{detail[:200]}")

    # ---------- 单篇处理 ----------

    def remember(
        self,
        page_name: str,
        response: httpx.Response,
        source_url: str,
        **extra: Any,
    ) -> None:
        saved = self.state["articles"].get(page_name) or {}
        self.state["articles"][page_name] = {
            **saved,
            "source_url": source_url,
            "etag": response.headers.get("etag", ""),
            "last_modified": response.headers.get("last-modified", ""),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }

    async def process_entry(
        self,
        official: httpx.AsyncClient,
        api: httpx.AsyncClient,
        entry: dict[str, str],
    ) -> bool:
        page_name = entry["page_name"]
        source_url = entry["source_url"]
        saved = self.state["articles"].get(page_name) or {}
        headers: dict[str, str] = {}
        # Validators belong to a specific URL. Do not send an ETag from an
        # old page URL after the official site changes its page location.
        if saved.get("source_url") == source_url and saved.get("etag"):
            headers["If-None-Match"] = str(saved["etag"])
        if saved.get("source_url") == source_url and saved.get("last_modified"):
            headers["If-Modified-Since"] = str(saved["last_modified"])

        response = await fetch_news_page(official, source_url, headers)
        if response.status_code == 304:
            log(f"官网未变化：{page_name}")
            self.state["stats"]["skipped_unchanged"] += 1
            self.remember(page_name, response, source_url)
            return False

        record = parse_topic_detail(response.text, source_url, entry)
        # Never carry a source-page image outside the official image allowlist
        # into the archive or the ingest API, even when it has HTML dimensions.
        record["images"] = [
            image
            for image in (record.get("images") or [])
            if isinstance(image, dict) and is_allowed_official_image_url(image.get("source_url"))
        ]
        source_hash = str(record.get("source_hash") or "")
        signature = content_signature(record)

        # 决定这篇要做什么：跳过 / 只记基线 / 提交。
        existing = self.production.get(page_name)
        # page_name is the stable source identity for this poller. If the
        # official page URL changes, keep the site's existing ID so the
        # editable source_url does not create a duplicate article.
        if existing and existing.get("id"):
            record["id"] = existing["id"]
        local = self.state["articles"].get(page_name)
        official_images = [image for image in (record.get("images") or []) if image.get("source_url")]
        cover = str((existing or {}).get("cover_url") or "")
        r2_base = f"{self.config.r2_public_base_url}/" if self.config.r2_public_base_url else ""
        cover_archived = bool(r2_base) and cover.startswith(r2_base)
        prod_images = int((existing or {}).get("image_count") or 0)
        # 站点图片未归档（封面不是本实例 R2 地址，或官网图片数量多于站点记录）时一并补归档。
        archived_images = (existing or {}).get("r2_image_count")
        if archived_images is None:
            # Backward compatibility with a site server that predates the
            # r2_image_count summary field.
            archived_images = prod_images if cover_archived and prod_images >= len(official_images) else 0
        needs_archive = bool(official_images) and (
            archived_images < len(official_images) or not cover_archived
        )
        official_sources = image_source_set(official_images)
        pushed_sources = image_source_set((local or {}).get("pushed_image_sources"))
        pending_sources = official_sources - pushed_sources

        if existing is None:
            decision, reason = "submit", "站点没有该篇（新增）"
        elif self.config.refresh_existing:
            decision, reason = "submit", "--refresh-existing 强制提交"
        elif needs_archive:
            decision, reason = "submit", f"站点图片未归档（{prod_images}/{len(official_images)} 张，封面非 R2）"
        elif local is None:
            decision, reason = "seed", "站点已有，首次建立基线"
        elif local.get("content_hash") == signature and local.get("pushed_hash") == signature and pending_sources:
            decision, reason = "submit", f"仍有 {len(pending_sources)} 张图片未成功提交"
        elif local.get("content_hash") == signature:
            decision, reason = "skip", "官网内容与基线一致"
        else:
            decision, reason = "submit", "官网内容有变化"

        if decision == "seed":
            log(f"{reason}，仅记录基线：{page_name}")
            self.state["stats"]["seeded"] += 1
            self.remember(
                page_name,
                response,
                source_url,
                source_hash=source_hash,
                content_hash=signature,
                seeded=True,
            )
            return False
        if decision == "skip":
            log(f"{reason}，跳过：{page_name}")
            self.state["stats"]["skipped_unchanged"] += 1
            self.remember(page_name, response, source_url, source_hash=source_hash, content_hash=signature)
            return False

        if self.config.dry_run:
            log(
                f"  [dry-run] 判定提交（{reason}）：{record['title'][:34]}"
                f" | 图片 {len(official_images)} 张 | 正文 {len(record.get('body_markdown') or '')} 字节"
            )
            for index, image in enumerate(official_images, start=1):
                log(f"  [dry-run] 图片 {index}：{image.get('source_url')}")
            return False

        original_images = [
            image for image in (record.get("images") or []) if isinstance(image, dict) and image.get("source_url")
        ]
        filtered_images = await filter_news_images(official, original_images)
        filtered_by_source = {str(image.get("source_url")): image for image in filtered_images}
        # Dimension probing intentionally filters malformed/unreachable images,
        # but omitting them from a full-snapshot upsert would delete an already
        # archived image. Keep a zero-metadata placeholder for every source so
        # the server can preserve it and the next pass can retry it.
        record["images"] = [
            filtered_by_source.get(str(image["source_url"]))
            or {
                **image,
                "width": image.get("width") or 0,
                "height": image.get("height") or 0,
                "bytes": 0,
                "sha256": "",
                "public_url": "",
            }
            for image in original_images
        ]

        if (
            not self.config.dry_run
            and not self.config.refresh_existing
            and not needs_archive
            and saved.get("pushed_hash") == signature
            and not image_source_set(record.get("images")) - pushed_sources
        ):
            log(f"本地记录一致，跳过提交：{page_name}")
            self.state["stats"]["skipped_existing"] += 1
            self.remember(
                page_name,
                response,
                source_url,
                source_hash=source_hash,
                content_hash=signature,
                pushed_image_sources=sorted(pushed_sources),
            )
            return False

        images: list[dict[str, Any]] = []
        for index, image in enumerate(record["images"], start=1):
            if index > 1:
                await asyncio.sleep(self.config.image_delay)
            try:
                data = await self.fetch_image_bytes(official, str(image["source_url"]), source_url)
                archived = self.archive_image(data)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    raise OfficialSiteBlocked() from exc
                log(f"  图片 {index} 处理失败：{type(exc).__name__} HTTP {exc.response.status_code}")
                images.append(
                    {
                        "kind": "remote",
                        "source_url": image["source_url"],
                        "alt_text": image.get("alt_text") or "",
                        "width": image.get("width") or 0,
                        "height": image.get("height") or 0,
                        "bytes": 0,
                        "sha256": "",
                        "public_url": "",
                    }
                )
                continue
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                log(f"  图片 {index} 处理失败：{type(exc).__name__} {exc}")
                # 仍提交一个没有 public_url 的占位项。服务端会按 source_url
                # 对账，并保留已有 R2 地址，避免一次临时失败删除旧图片。
                images.append(
                    {
                        "kind": "remote",
                        "source_url": image["source_url"],
                        "alt_text": image.get("alt_text") or "",
                        "width": image.get("width") or 0,
                        "height": image.get("height") or 0,
                        "bytes": 0,
                        "sha256": "",
                        "public_url": "",
                    }
                )
                continue
            images.append(
                {
                    "kind": "remote",
                    "source_url": image["source_url"],
                    "alt_text": image.get("alt_text") or "",
                    "width": archived["width"] or image.get("width") or 0,
                    "height": archived["height"] or image.get("height") or 0,
                    "bytes": archived["bytes"],
                    "sha256": archived["sha256"],
                    "public_url": archived["public_url"],
                }
            )
            log(f"  图片 {index} 已归档 R2：{archived['bytes']} 字节")

        article = {
            "id": record["id"],
            "source": record["source"],
            "page_name": page_name,
            "title": record["title"],
            "published_at": record["published_at"],
            "category": record.get("category") or "",
            "tags": record.get("tags") or [],
            "summary": record.get("summary") or "",
            "body_markdown": record.get("body_markdown") or "",
            "source_url": record["source_url"],
            "source_hash": source_hash,
            "images": images,
        }
        if self.config.dry_run:
            return False

        await self.submit(api, article)
        self.remember(
            page_name,
            response,
            source_url,
            source_hash=source_hash,
            content_hash=signature,
            pushed_hash=signature,
            pushed_image_sources=sorted(archived_image_source_set(images)),
            title=article["title"],
            pushed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.state["stats"]["pushed"] += 1
        log(f"已提交：{page_name} 《{article['title'][:36]}》图片 {len(images)}/{len(record['images'])} 张")
        return True

    # ---------- 单趟 ----------

    async def run_pass(self) -> dict[str, Any]:
        config = self.config
        handled = 0
        proxy_url, node_label = self.choose_proxy()
        self.current_node = node_label
        log(f"本趟出口：{node_label}")
        proxy = proxy_url or None
        async with httpx.AsyncClient(
            timeout=config.timeout,
            headers=NEWS_HEADERS,
            proxy=proxy,
            trust_env=False,
        ) as official:
            async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=False, trust_env=False) as api:
                try:
                    self.production = await self.load_production_index(api)
                except httpx.HTTPError as exc:
                    raise SiteApiError("读取站点新闻索引失败") from exc
                log(f"站点已有新闻 {len(self.production)} 条（首页比对基准）")
                offset = 0
                for page in range(config.max_pages):
                    try:
                        response = await fetch_news_page(official, news_listing_url(offset))
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 403:
                            raise OfficialSiteBlocked() from exc
                        raise
                    entries = parse_topic_listing(response.text, NEWS_TOPICS_URL)
                    log(f"第 {page + 1} 页列表：{len(entries)} 篇")
                    for entry in entries:
                        if config.only and entry["page_name"] not in config.only:
                            continue
                        if config.limit and handled >= config.limit:
                            break
                        try:
                            await self.process_entry(official, api, entry)
                            handled += 1
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code == 403:
                                raise OfficialSiteBlocked() from exc
                            log(f"  处理失败 {entry['page_name']}：HTTP {exc.response.status_code}")
                            self.state["stats"]["errors"] += 1
                        except OfficialSiteBlocked:
                            raise
                        except SiteApiError:
                            raise
                        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                            log(f"  处理失败 {entry['page_name']}：{type(exc).__name__} {exc}")
                            self.state["stats"]["errors"] += 1
                        await asyncio.sleep(config.article_delay)
                    if config.limit and handled >= config.limit:
                        break
                    next_offset = topic_next_offset(response.text, offset)
                    if next_offset is None:
                        break
                    offset = next_offset
        self.state["stats"]["passes"] += 1
        self.state["last_pass_at"] = time.time()
        self.state["backoff_until"] = 0.0
        return {"handled": handled, "stats": self.state["stats"]}

    async def run(self) -> int:
        lock = acquire_state_lock(self.config.state_file)
        try:
            return await self._run()
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()

    async def _run(self) -> int:
        config = self.config
        while True:
            remaining = float(self.state.get("backoff_until") or 0) - time.time()
            if remaining > 0 and not config.once:
                log(f"官网上次返回 403，退避中（剩余约 {remaining / 60:.0f} 分钟）")
                await asyncio.sleep(min(remaining, 300))
                continue
            failed = False
            try:
                result = await self.run_pass()
                self.retry_count = 0
                stats = self.state["stats"]
                log(
                    f"本趟完成：检查 {result['handled']} 篇 | 累计提交 {stats['pushed']}"
                    f" | 新增基线 {stats['seeded']} | 官网未变 {stats['skipped_unchanged']}"
                    f" | 内容未变 {stats['skipped_existing']} | 错误 {stats['errors']}"
                )
            except OfficialSiteBlocked:
                failed = True
                self.record_hint(self.current_node)
                if self.config.nodes_file and self.retry_count < 1:
                    self.retry_count += 1
                    log(f"节点「{self.current_node}」返回 403，换一个节点立即重试")
                    continue
                self.retry_count = 0
                self.state["backoff_until"] = time.time() + config.backoff_seconds
                log(f"官网返回 403，进入 {config.backoff_seconds / 60:.0f} 分钟退避")
            except (httpx.HTTPError, PollerConfigError, RuntimeError, ValueError) as exc:
                failed = True
                if self.config.nodes_file and isinstance(exc, httpx.HTTPError) and self.retry_count < 1:
                    self.record_hint(self.current_node)
                    self.retry_count += 1
                    log(f"节点「{self.current_node}」异常（{type(exc).__name__}），换一个节点立即重试")
                    continue
                self.retry_count = 0
                log(f"本趟异常：{type(exc).__name__} {exc}")
            finally:
                save_state(config.state_file, self.state)
            if config.once:
                return 1 if failed else 0
            if failed:
                # 代理刚重启、网络抖动之类的问题不必等满整个休息窗口
                wait = min(config.rest_min_seconds, 300.0)
                log(f"本趟失败，{wait / 60:.0f} 分钟后重试")
            else:
                wait = random.uniform(config.rest_min_seconds, config.rest_max_seconds)
                log(
                    f"休息 {int(wait // 60)} 分 {int(wait % 60)} 秒"
                    f"（随机区间 {config.rest_min_seconds / 60:g}–{config.rest_max_seconds / 60:g} 分钟）"
                )
            await asyncio.sleep(wait)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except PollerConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    log(
        "启动轮询："
        f"出口 {config.proxy} | API {config.base_url} | 每趟最多 {config.max_pages} 页"
        f" | 文章间隔 {config.article_delay:g}s | 图片间隔 {config.image_delay:g}s"
        f" | 趟间隔 {config.rest_min_seconds / 60:g}–{config.rest_max_seconds / 60:g} 分钟（随机）"
        + ("（dry-run）" if config.dry_run else "")
    )
    try:
        return asyncio.run(OfficialNewsPoller(config).run())
    except PollerConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log("已中断")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
