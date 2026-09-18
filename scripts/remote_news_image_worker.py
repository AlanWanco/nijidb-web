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
import fcntl
import hashlib
import ipaddress
import json
import math
import re
import os
import secrets
import signal
import stat
import sys
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.news import news_image_dimensions_allowed  # noqa: E402
from app.news_fetch import image_dimensions_from_bytes, is_avif_bytes  # noqa: E402
from logfmt import format_message  # noqa: E402

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - reported when the script is run
    boto3 = None
    ClientError = Exception


DEFAULT_PAGE_HOSTS = {
    "www.lovelive-anime.jp",
    "lovelive-anime.jp",
    "lovelive-as.bushimo.jp",
}
DEFAULT_IMAGE_HOSTS = DEFAULT_PAGE_HOSTS | {
    "img.sunrise-inc.co.jp",
    "img.sunrise-inc.jp",
}
IMAGE_MAX_BYTES = 20 * 1024 * 1024
JSON_MAX_BYTES = 8 * 1024 * 1024
STATE_MAX_BYTES = 8 * 1024 * 1024
STATE_KEY_MAX_LENGTH = 230
MAX_FAILURE_RECORDS = 4096
MAX_UPLOAD_RECORDS = 2048
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


def safe_url_path(value: str) -> bool:
    try:
        path = unquote(urlparse(value).path)
    except (TypeError, ValueError):
        return False
    parts = path.split("/")
    return (
        not any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        and "?" not in path
        and "#" not in path
        and "\\" not in path
        and all(part not in {".", ".."} for part in parts if part)
        and all(part for part in parts[1:-1])
    )


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


def valid_image_host(value: Any) -> bool:
    host = str(value or "").strip().lower()
    if (
        not host
        or len(host) > 253
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in host)
        or "\\" in host
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host)
    ):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


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
        # Accessing .port makes urlparse reject malformed ports too.
        base.port
        public.port
        endpoint_url.port
    except (TypeError, ValueError) as exc:
        raise WorkerConfigError("网站或 R2 地址格式无效") from exc
    if any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
        for value in (base_url, public_base, endpoint)
    ):
        raise WorkerConfigError("网站或 R2 地址格式无效")
    if (
        base.scheme not in {"http", "https"}
        or not base.hostname
        or any(character.isspace() for character in base.hostname)
        or base.netloc.endswith(":")
        or base.port == 0
        or base.username
        or base.password
        or base.query
        or "?" in base_url
        or base.fragment
        or "#" in base_url
        or not safe_url_path(base_url)
    ):
        raise WorkerConfigError("NIJIDB_BASE_URL 必须是无凭据且不带查询参数的 HTTP/HTTPS 地址")
    if (
        public.scheme not in {"http", "https"}
        or not public.hostname
        or any(character.isspace() for character in public.hostname)
        or public.netloc.endswith(":")
        or public.port == 0
        or public.username
        or public.password
        or public.query
        or "?" in public_base
        or public.fragment
        or "#" in public_base
        or not safe_url_path(public_base)
    ):
        raise WorkerConfigError("R2_PUBLIC_BASE_URL 必须是无凭据且不带查询参数的 HTTP/HTTPS 地址")
    if (
        endpoint_url.scheme not in {"http", "https"}
        or not endpoint_url.hostname
        or any(character.isspace() for character in endpoint_url.hostname)
        or endpoint_url.netloc.endswith(":")
        or endpoint_url.port == 0
        or endpoint_url.username
        or endpoint_url.password
        or endpoint_url.query
        or "?" in endpoint
        or endpoint_url.fragment
        or "#" in endpoint
        or not safe_url_path(endpoint)
    ):
        raise WorkerConfigError("R2_ENDPOINT 必须是无凭据且不带查询参数的 HTTP/HTTPS 地址")
    if same_http_origin(public_base, endpoint):
        raise WorkerConfigError("R2_PUBLIC_BASE_URL 不能直接使用 R2 S3 API Endpoint")
    if (
        len(prefix) > 256
        or "\\" in prefix
        or prefix
        and any(
            part in {"", ".", ".."}
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
            for part in prefix.split("/")
        )
    ):
        raise WorkerConfigError("R2_IMAGE_PREFIX 格式无效")
    if len(api_key) > 256:
        raise WorkerConfigError("NIJIDB_INGEST_API_KEY 不能超过 256 个字符")

    delay = args.delay_seconds if args.delay_seconds is not None else env_number("NEWS_WORKER_DELAY_SECONDS", 30, 30)
    if not math.isfinite(delay) or delay < 30:
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
    invalid_hosts = sorted(host for host in hosts if not valid_image_host(host))
    if invalid_hosts:
        raise WorkerConfigError("图片主机格式无效")
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
        raise WorkerConfigError(f"状态文件不能是符号链接：{path}")
    if not path.exists():
        return default_state()
    try:
        raw = _read_state_bytes(path)
        if raw is None:
            raise OSError("状态文件不是普通文件")
        if len(raw) > STATE_MAX_BYTES:
            raise WorkerConfigError(f"状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB 限制：{path}")
        value = json.loads(raw.decode("utf-8"))
    except WorkerConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WorkerConfigError(f"无法读取状态文件：{path}") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise WorkerConfigError(f"状态文件版本不支持：{path}")
    state = default_state()
    state.update(value)
    if not isinstance(state["article_ids"], list) or not isinstance(state["failures"], dict) or not isinstance(state["uploads"], dict):
        raise WorkerConfigError(f"状态文件格式无效：{path}")
    state["article_ids"] = [
        str(article_id).strip()
        for article_id in state["article_ids"]
        if bounded_state_text(article_id, 256)
    ][:1000]
    state["index_page"] = state_int(state.get("index_page"), 0, 0, 1000)
    state["page_count"] = state_int(state.get("page_count"), 0, 0, 1000)
    state["article_index"] = state_int(state.get("article_index"), 0, 0, 1000)
    state["image_index"] = state_int(state.get("image_index"), 0, 0, 1000)
    state["last_index_refresh"] = state_float(state.get("last_index_refresh"))
    state["pass_complete_at"] = state_float(state.get("pass_complete_at"))
    state["next_pass_at"] = state_float(state.get("next_pass_at"))
    state["failures"] = {
        str(key).strip(): value
        for key, value in state["failures"].items()
        if bounded_state_text(key, STATE_KEY_MAX_LENGTH) and isinstance(value, dict)
    }
    state["uploads"] = {
        str(key).strip(): value
        for key, value in state["uploads"].items()
        if bounded_state_text(key, STATE_KEY_MAX_LENGTH) and isinstance(value, dict)
    }
    return normalize_loaded_state(state)


def acquire_run_lock(state_path: Path):
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    if lock_path.parent.is_symlink():
        raise WorkerConfigError("状态文件目录不能是符号链接")
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
        raise WorkerConfigError("已有相同状态文件的新闻图片 Worker 正在运行") from exc
    return handle


def save_state(path: Path, state: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise WorkerConfigError("状态文件目录不能是符号链接")
    normalized = normalize_loaded_state(state)
    state.clear()
    state.update(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > STATE_MAX_BYTES:
        raise WorkerConfigError(f"状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB 限制：{path}")
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
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
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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
    if is_avif_bytes(data):
        return ".avif", "image/avif"
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    return image_dimensions_from_bytes(data)


def is_r2_public_url(value: Any, base_url: str, prefix: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        base = urlparse(base_url)
        candidate = urlparse(raw)
        base.port
        candidate.port
    except (TypeError, ValueError):
        return False
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        or "\\" in raw
        or base.scheme not in {"http", "https"}
        or not base.hostname
        or base.username
        or base.password
        or base.query
        or "?" in base_url
        or base.fragment
        or "#" in base_url
        or base.netloc.endswith(":")
        or base.port == 0
        or candidate.scheme != base.scheme
        or candidate.netloc != base.netloc
        or candidate.netloc.endswith(":")
        or candidate.port == 0
        or candidate.username
        or candidate.password
        or candidate.query
        or "?" in raw
        or candidate.fragment
        or "#" in raw
    ):
        return False
    base_path = unquote(base.path).rstrip("/")
    decoded_path = unquote(candidate.path)
    base_parts = base_path.strip("/").split("/") if base_path.strip("/") else []
    prefix = str(prefix or "").strip("/")
    prefix_parts = prefix.split("/") if prefix else []
    candidate_parts = decoded_path.strip("/").split("/") if decoded_path.strip("/") else []
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in base_path)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        or "\\" in base_path
        or "\\" in decoded_path
        or decoded_path.endswith("/")
        or any(part in {"", ".", ".."} for part in (*base_parts, *prefix_parts, *candidate_parts))
    ):
        return False
    expected = f"{base_path}/{prefix}" if prefix else base_path
    if expected:
        expected = expected.rstrip("/")
        if not decoded_path.startswith(f"{expected}/"):
            return False
    elif not candidate_parts:
        return False
    return bool(candidate_parts)


def source_key(article_id: str, source_url: str) -> str:
    raw = f"{article_id}:{source_url}"
    if len(raw) <= STATE_KEY_MAX_LENGTH:
        return raw
    return f"{article_id[:64]}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def bounded_state_text(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
    ):
        return ""
    return text


def state_int(value: Any, default: int = 0, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if minimum <= number <= maximum else default


def state_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def source_host(source_url: str) -> str:
    try:
        return (urlparse(source_url).hostname or "").lower()
    except ValueError:
        return ""


def normalize_state_maps(state: dict[str, Any]) -> None:
    """Keep retry/pending-upload maps bounded and discard untrusted fields."""
    failures = state.get("failures")
    if not isinstance(failures, dict):
        failures = {}
    normalized_failures: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in list(failures.items())[-MAX_FAILURE_RECORDS:]:
        key = bounded_state_text(raw_key, STATE_KEY_MAX_LENGTH)
        if not key or not isinstance(raw_value, dict):
            continue
        status = raw_value.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            status = None
        normalized_failures[key] = {
            "attempts": state_int(raw_value.get("attempts"), 1, 1, 2**31 - 1),
            "status": status,
            "next_attempt_at": state_float(raw_value.get("next_attempt_at")),
            "last_error": re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw_value.get("last_error") or ""))[:240],
        }
    state["failures"] = normalized_failures

    uploads = state.get("uploads")
    if not isinstance(uploads, dict):
        uploads = {}
    normalized_uploads: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in list(uploads.items())[-MAX_UPLOAD_RECORDS:]:
        key = bounded_state_text(raw_key, STATE_KEY_MAX_LENGTH)
        if not key or not isinstance(raw_value, dict):
            continue
        sha256 = str(raw_value.get("sha256") or "").strip().lower()
        normalized_uploads[key] = {
            "public_url": bounded_state_text(raw_value.get("public_url"), 2048),
            "sha256": sha256 if re.fullmatch(r"[0-9a-f]{64}", sha256) else "",
            "bytes": state_int(raw_value.get("bytes"), 0, 0, IMAGE_MAX_BYTES),
            "width": state_int(raw_value.get("width"), 0, 0, 100000),
            "height": state_int(raw_value.get("height"), 0, 0, 100000),
        }
    state["uploads"] = normalized_uploads


def normalize_loaded_state(state: dict[str, Any]) -> dict[str, Any]:
    defaults = default_state()
    # Ignore unknown top-level fields so a poisoned state file cannot be copied
    # back out indefinitely by every atomic save.
    normalized = {key: state.get(key, default) for key, default in defaults.items()}
    normalized["version"] = 1
    seen_ids: set[str] = set()
    article_ids: list[str] = []
    raw_article_ids = normalized.get("article_ids")
    if not isinstance(raw_article_ids, list):
        raw_article_ids = []
    for raw_id in raw_article_ids:
        article_id = bounded_state_text(raw_id, 256)
        if article_id and article_id not in seen_ids:
            seen_ids.add(article_id)
            article_ids.append(article_id)
    normalized["article_ids"] = article_ids[:1000]
    normalized["index_page"] = state_int(normalized.get("index_page"), 0, 0, 1000)
    normalized["page_count"] = state_int(normalized.get("page_count"), 0, 0, 1000)
    normalized["article_index"] = state_int(normalized.get("article_index"), 0, 0, 1000)
    normalized["image_index"] = state_int(normalized.get("image_index"), 0, 0, 1000)
    normalized["last_index_refresh"] = state_float(normalized.get("last_index_refresh"))
    normalized["pass_complete_at"] = state_float(normalized.get("pass_complete_at"))
    normalized["next_pass_at"] = state_float(normalized.get("next_pass_at"))
    normalize_state_maps(normalized)
    return normalized


class NewsImageWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.state = load_state(config.state_file)
        self.stop_requested = False
        # 可选单条固定代理，用于官网直连被拒的运行环境；不做任何轮换。
        proxy = os.getenv("NEWS_WORKER_PROXY", "").strip()
        if proxy:
            try:
                parsed_proxy = urlparse(proxy)
                proxy_port = parsed_proxy.port
            except (TypeError, ValueError) as exc:
                raise WorkerConfigError("NEWS_WORKER_PROXY 地址格式无效") from exc
            if (
                any(ord(character) < 0x20 or ord(character) == 0x7F for character in proxy)
                or "\\" in proxy
                or parsed_proxy.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed_proxy.hostname
                or any(character.isspace() for character in parsed_proxy.hostname)
                or parsed_proxy.netloc.endswith(":")
                or proxy_port is not None and not 1 <= proxy_port <= 65535
                or parsed_proxy.username
                or parsed_proxy.password
                or parsed_proxy.query
                or parsed_proxy.fragment
                or parsed_proxy.path not in {"", "/"}
            ):
                raise WorkerConfigError("NEWS_WORKER_PROXY 必须是无凭据的完整代理地址")
            handlers: dict[str, str] = {"http": proxy, "https": proxy}
        else:
            handlers = {}
        self.proxy_enabled = bool(proxy)
        self.opener = build_opener(ProxyHandler(handlers), NoRedirectHandler())
        # The optional proxy is only for official image requests. Keep the
        # Nijidb API direct so the ingest key is not sent through that proxy.
        self.api_opener = build_opener(ProxyHandler({}), NoRedirectHandler())
        self.base_parts = urlparse(config.base_url)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=config.r2_endpoint,
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name=os.getenv("R2_REGION", "auto"),
        )

    def log(self, message: str) -> None:
        print(format_message(f"[news-worker] {message}"), flush=True)

    def request_bytes(
        self,
        url: str,
        headers: dict[str, str],
        max_bytes: int,
        allowed_hosts: set[str] | frozenset[str],
        require_https: bool,
        opener=None,
    ) -> bytes:
        try:
            initial = urlparse(url)
            initial_port = effective_http_port(initial)
            initial_scheme = initial.scheme
        except (TypeError, ValueError) as exc:
            raise WorkerRequestError("请求地址无效") from exc
        current = url
        for _ in range(5):
            try:
                parsed = urlparse(current)
                port = parsed.port
                decoded_path = unquote(parsed.path)
                path_parts = decoded_path.split("/")
                safe_path = (
                    all(part not in {".", ".."} for part in path_parts if part)
                    and all(part for part in path_parts[1:-1])
                )
                schemes = {"https"} if require_https else {"http", "https"}
                valid = (
                    not any(ord(character) < 0x20 or ord(character) == 0x7F for character in current)
                    and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
                    and "#" not in current
                    and not ("?" in current and not parsed.query)
                    and parsed.scheme in schemes
                    and parsed.scheme == initial_scheme
                    and effective_http_port(parsed) == initial_port
                    and parsed.hostname in allowed_hosts
                    and parsed.hostname is not None
                    and not any(character.isspace() for character in parsed.hostname)
                    and not parsed.netloc.endswith(":")
                    and port != 0
                    and safe_path
                    and not parsed.username
                    and not parsed.password
                    and "\\" not in current
                    and "\\" not in decoded_path
                    and (parsed.scheme != "https" or port in {None, 443})
                )
            except (TypeError, ValueError) as exc:
                raise WorkerRequestError("请求地址无效") from exc
            if not valid:
                raise WorkerRequestError("请求重定向到了不允许的地址")
            request = Request(current, headers=headers, method="GET")
            try:
                response = (opener or self.opener).open(request, timeout=self.config.request_timeout_seconds)
            except HTTPError as exc:
                if exc.code in REDIRECT_CODES:
                    location = exc.headers.get("Location", "")
                    if not location:
                        raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
                    try:
                        current = urljoin(current, location)
                    except (TypeError, ValueError) as location_error:
                        raise WorkerRequestError("请求重定向地址无效", exc.code) from location_error
                    continue
                raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
            except (TimeoutError, URLError, OSError) as exc:
                raise WorkerRequestError("网络请求失败") from exc
            with response:
                try:
                    status = int(getattr(response, "status", 200))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WorkerRequestError("HTTP 响应状态无效") from exc
                if not 100 <= status <= 599:
                    raise WorkerRequestError("HTTP 响应状态无效")
                if status in REDIRECT_CODES:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise WorkerRequestError(f"HTTP {status}", status)
                    try:
                        current = urljoin(current, location)
                    except (TypeError, ValueError) as location_error:
                        raise WorkerRequestError("请求重定向地址无效", status) from location_error
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
            opener=self.api_opener,
        )
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
            response = self.api_opener.open(request, timeout=self.config.request_timeout_seconds)
        except HTTPError as exc:
            raise WorkerRequestError(f"HTTP {exc.code}", exc.code) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise WorkerRequestError("网络请求失败") from exc
        with response:
            try:
                status = int(getattr(response, "status", 200))
            except (TypeError, ValueError, OverflowError) as exc:
                raise WorkerRequestError("HTTP 响应状态无效") from exc
            if not 100 <= status <= 599:
                raise WorkerRequestError("HTTP 响应状态无效")
            response_body = response.read(JSON_MAX_BYTES + 1)
        if len(response_body) > JSON_MAX_BYTES:
            raise WorkerRequestError("导入响应过大")
        if status < 200 or status >= 300:
            raise WorkerRequestError(f"HTTP {status}", status)
        try:
            value = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
        except (TypeError, ValueError, OverflowError):
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
        current_page = state_int(self.state.get("index_page"), 0, 0, 1000)
        page_count = state_int(self.state.get("page_count"), 1, 1, 1000)
        if current_page >= page_count:
            self.complete_pass()
            return False
        return self.load_listing_page(current_page + 1)

    def prepare_pass(self) -> bool:
        now = time.time()
        pass_complete_at = state_float(self.state.get("pass_complete_at"))
        if pass_complete_at:
            if now < state_float(self.state.get("next_pass_at")):
                return False
            self.refresh_index()
        elif not self.state.get("index_page"):
            self.refresh_index()
        while not self.state.get("article_ids"):
            if not self.advance_page_or_pass():
                return False
        return True

    def complete_article(self) -> None:
        self.state["article_index"] = state_int(self.state.get("article_index"), 0) + 1
        self.state["image_index"] = 0
        save_state(self.config.state_file, self.state)

    def complete_image(self) -> None:
        self.state["image_index"] = state_int(self.state.get("image_index"), 0) + 1
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
            next_attempt_at = float(value.get("next_attempt_at") or 0)
        except (TypeError, ValueError, OverflowError):
            return True
        return not math.isfinite(next_attempt_at) or time.time() >= next_attempt_at

    def record_failure(self, key: str, error: Exception, permanent: bool = False) -> None:
        previous = self.failure_record(key) or {}
        try:
            attempts = int(previous.get("attempts") or 0) + 1
        except (TypeError, ValueError, OverflowError):
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

    def valid_image_source(self, source_url: str) -> bool:
        raw = str(source_url or "").strip()
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        return (
            bool(raw)
            and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
            and "#" not in raw
            and not ("?" in raw and not parsed.query)
            and "\\" not in raw
            and parsed.scheme == "https"
            and parsed.hostname in self.config.image_hosts
            and port in {None, 443}
            and not parsed.netloc.endswith(":")
            and not parsed.username
            and not parsed.password
            and safe_url_path(raw)
        )

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
            if not self.valid_image_source(source_url):
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
                except (TypeError, ValueError, OverflowError):
                    value = 0
                item[field] = value if 0 <= value <= (100000 if field != "bytes" else IMAGE_MAX_BYTES) else 0
            sha256 = str(raw.get("sha256") or "").strip().lower()
            if len(sha256) == 64:
                item["sha256"] = sha256
            if isinstance(pending, dict):
                pending_public = str(pending.get("public_url") or "").strip()
                if is_r2_public_url(pending_public, self.config.r2_public_base_url, self.config.r2_prefix):
                    item["public_url"] = pending_public
                item["bytes"] = state_int(pending.get("bytes"), item["bytes"], 0, IMAGE_MAX_BYTES)
                pending_sha256 = str(pending.get("sha256") or "").strip().lower()
                if len(pending_sha256) == 64 and all(character in "0123456789abcdef" for character in pending_sha256):
                    item["sha256"] = pending_sha256
                if pending.get("width") not in (None, ""):
                    item["width"] = state_int(pending.get("width"), item["width"], 0, 100000)
                if pending.get("height") not in (None, ""):
                    item["height"] = state_int(pending.get("height"), item["height"], 0, 100000)
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
        if len(data) > IMAGE_MAX_BYTES:
            raise InvalidImageError("图片超过 20 MB 限制")
        signature = image_signature(data)
        dimensions = image_dimensions(data)
        if not signature or not dimensions:
            raise InvalidImageError("响应不是有效的支持图片")
        if not news_image_dimensions_allowed(*dimensions):
            raise InvalidImageError("图片尺寸不在允许范围内")
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
        try:
            parsed = urlparse(source_url)
            parsed.port
        except (TypeError, ValueError) as exc:
            raise InvalidImageError("图片来源地址无效") from exc
        if parsed.scheme != "https" or parsed.fragment or "#" in source_url:
            raise InvalidImageError("图片来源不是无片段 HTTPS 地址")
        if not host or host not in self.config.image_hosts:
            raise InvalidImageError("图片主机不在允许列表")
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja-JP;q=0.8,ja;q=0.7",
            "User-Agent": WORKER_USER_AGENT,
        }
        try:
            referer = urlparse(str(article_source_url).strip())
            referer.port
            referer_valid = (
                referer.scheme == "https"
                and referer.hostname in DEFAULT_PAGE_HOSTS
                and referer.port in {None, 443}
                and not referer.username
                and not referer.password
                and "#" not in str(article_source_url)
                and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in str(article_source_url))
                and "\\" not in str(article_source_url)
            )
        except (TypeError, ValueError):
            referer_valid = False
        if referer_valid:
            headers["Referer"] = str(article_source_url).strip()
        return self.request_bytes(source_url, headers, IMAGE_MAX_BYTES, self.config.image_hosts, True), host

    def step(self) -> bool:
        if not self.prepare_pass():
            return False
        article_ids = self.state.get("article_ids", [])
        article_index = state_int(self.state.get("article_index"), 0)
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
        image_index = state_int(self.state.get("image_index"), 0)
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
    lock = None
    try:
        config = build_config(args)
        lock = acquire_run_lock(config.state_file)
        worker = NewsImageWorker(config)
    except WorkerConfigError as exc:
        if lock is not None:
            lock.close()
        print(f"[news-worker] 配置错误：{exc}", file=sys.stderr)
        return 2

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        worker.stop_requested = True
        worker.log(f"收到停止信号 {signum}，将在当前请求后退出")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    worker.log(f"启动：站点已配置，出口 {'固定代理' if worker.proxy_enabled else '直连'}，图片间隔 {config.delay_seconds:g}s")
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop_requested = True
        save_state(config.state_file, worker.state)
    except (WorkerRequestError, RuntimeError, OSError) as exc:
        print(f"[news-worker] 运行失败：{str(exc)[:240]}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
