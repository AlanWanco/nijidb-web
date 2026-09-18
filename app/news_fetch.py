"""Bounded official-news fetching; no arbitrary URL proxy or private-network requests."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse

import httpcore
import httpx

from app.news import news_image_dimensions_allowed

OFFICIAL_PAGE_HOSTS = {"www.lovelive-anime.jp", "lovelive-anime.jp", "lovelive-as.bushimo.jp"}
# Official pages also serve historical assets from Sunrise image hosts. Keep
# these separate from page hosts so URL validation never makes a CDN usable as
# a news source page.
OFFICIAL_IMAGE_HOSTS = OFFICIAL_PAGE_HOSTS | {"img.sunrise-inc.co.jp", "img.sunrise-inc.jp"}
# Backward-compatible alias for callers that only need the page allowlist.
OFFICIAL_HOSTS = OFFICIAL_PAGE_HOSTS
OFFICIAL_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,ja-JP;q=0.8,ja;q=0.7",
}
NEWS_HEADERS = {
    **OFFICIAL_BROWSER_HEADERS,
    "Referer": "https://www.lovelive-anime.jp/nijigasaki/topics.php",
}
PUBLIC_IMAGE_MAX_BYTES = 20 * 1024 * 1024
PUBLIC_IMAGE_TIMEOUT_SECONDS = 30
PUBLIC_IMAGE_DNS_TIMEOUT_SECONDS = 5


def is_valid_public_http_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2000 or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        return False
    try:
        parsed = urlparse(raw)
        port = parsed.port
        hostname = parsed.hostname or ""
        decoded_path = unquote(parsed.path)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(hostname)
        and not any(character.isspace() for character in hostname)
        and (port is None or 1 <= port <= 65535)
        and not parsed.netloc.endswith(":")
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and "#" not in raw
        and not ("?" in raw and not parsed.query)
        and "\\" not in raw
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        and "?" not in decoded_path
        and "#" not in decoded_path
        and all(part not in {".", ".."} for part in decoded_path.split("/") if part)
        and all(part for part in decoded_path.split("/")[1:-1])
    )


def resolve_public_image_addresses(url: str) -> list[str]:
    if not is_valid_public_http_url(url):
        raise ValueError("图片直链必须是有效的 HTTP/HTTPS 地址")
    parsed = urlparse(url)
    hostname = str(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("图片直链域名无法解析") from exc
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos if info[4]))
    if not addresses:
        raise ValueError("图片直链必须解析到公网地址")
    try:
        all_global = all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError as exc:
        raise ValueError("图片直链必须解析到公网地址") from exc
    if not all_global:
        raise ValueError("图片直链必须解析到公网地址")
    return addresses


class _PinnedAddressBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, address: str):
        self.address = address
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self.backend.connect_tcp(
            self.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self.backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class _PinnedAddressTransport(httpx.AsyncHTTPTransport):
    def __init__(self, address: str):
        super().__init__(trust_env=False, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpcore.default_ssl_context(),
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=_PinnedAddressBackend(address),
        )


async def fetch_public_image_bytes(url: str, max_bytes: int = PUBLIC_IMAGE_MAX_BYTES) -> tuple[bytes, str]:
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(resolve_public_image_addresses, url),
            timeout=PUBLIC_IMAGE_DNS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("图片直链域名解析超时") from exc
    last_error: Exception | None = None
    for address in addresses:
        try:
            transport = _PinnedAddressTransport(address)
            async with httpx.AsyncClient(
                transport=transport,
                timeout=PUBLIC_IMAGE_TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={"Accept": "image/*,*/*;q=0.8"},
                trust_env=False,
            ) as client:
                async with asyncio.timeout(PUBLIC_IMAGE_TIMEOUT_SECONDS):
                    async with client.stream("GET", url) as response:
                        if 300 <= response.status_code < 400:
                            raise ValueError("图片直链不允许重定向")
                        response.raise_for_status()
                        content_length = response.headers.get("content-length", "")
                        if content_length:
                            try:
                                declared_length = int(content_length)
                            except ValueError as exc:
                                raise ValueError("图片响应长度无效") from exc
                            if declared_length < 0:
                                raise ValueError("图片响应长度无效")
                            if declared_length > max_bytes:
                                raise ValueError("图片超过 20 MB 限制")
                        content = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            if len(content) + len(chunk) > max_bytes:
                                raise ValueError("图片超过 20 MB 限制")
                            content.extend(chunk)
                        return bytes(content), response.headers.get("content-type", "")
        except ValueError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            last_error = exc
    raise RuntimeError("图片直链读取失败") from last_error


def _safe_url_path(parsed) -> bool:
    try:
        path = unquote(parsed.path)
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


def _has_empty_port(parsed) -> bool:
    return parsed.netloc.endswith(":")


def is_allowed_official_image_url(url: Any) -> bool:
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        bool(raw)
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        and parsed.scheme == "https"
        and hostname in OFFICIAL_IMAGE_HOSTS
        and port in {None, 443}
        and not _has_empty_port(parsed)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and "#" not in raw
        and not ("?" in raw and not parsed.query)
        and "\\" not in raw
        and _safe_url_path(parsed)
    )


def validate_news_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        valid = (
            not any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
            and parsed.scheme == "https"
            and parsed.hostname in OFFICIAL_PAGE_HOSTS
            and parsed.port in {None, 443}
            and not _has_empty_port(parsed)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and "#" not in url
            and not ("?" in url and not parsed.query)
            and "\\" not in url
            and _safe_url_path(parsed)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("刷新仅允许 HTTPS 官网来源地址")
    return url


def describe_news_fetch_error(error: Exception) -> str:
    """Return a useful, bounded message without exposing request details."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 403:
            return "官网返回 HTTP 403（访问被拒绝）"
        if status == 404:
            return "官网返回 HTTP 404（页面不存在）"
        if status == 429:
            return "官网返回 HTTP 429（请求过于频繁，请稍后重试）"
        if status >= 500:
            return f"官网返回 HTTP {status}（官网暂时不可用）"
        return f"官网返回 HTTP {status}"
    if isinstance(error, httpx.TimeoutException):
        return "连接官网超时（30 秒）"
    if isinstance(error, httpx.NetworkError):
        return "连接官网失败（网络或 TLS 错误）"
    if isinstance(error, ValueError):
        return str(error) or "官网内容无法解析"
    if isinstance(error, RuntimeError):
        return str(error) or "官网请求重试失败"
    return "未知的官网读取错误"


def _positive_dimensions(width: int, height: int) -> tuple[int, int] | None:
    return (width, height) if width > 0 and height > 0 else None


def _avif_ftyp_brands(data: bytes) -> tuple[bytes, list[bytes]] | None:
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    box_size = int.from_bytes(data[:4], "big")
    if box_size == 1:
        if len(data) < 24:
            return None
        box_end = int.from_bytes(data[8:16], "big")
        if box_end and box_end < 24:
            return None
        major_start = 16
        compatible_start = 24
    else:
        if box_size not in {0} and box_size < 16:
            return None
        box_end = box_size or len(data)
        major_start = 8
        compatible_start = 16
    end = min(len(data), box_end)
    if end < compatible_start or major_start + 4 > end:
        return None
    major_brand = data[major_start : major_start + 4]
    compatible_brands = [
        data[position : position + 4]
        for position in range(compatible_start, end - 3, 4)
    ]
    return major_brand, compatible_brands


def is_avif_bytes(data: bytes) -> bool:
    brands = _avif_ftyp_brands(data)
    return bool(brands) and (
        brands[0] in {b"avif", b"avis"}
        or bool({b"avif", b"avis"}.intersection(brands[1]))
    )


def _avif_dimensions(data: bytes) -> tuple[int, int] | None:
    if not is_avif_bytes(data):
        return None

    def find_ispe(start: int, end: int, depth: int = 0) -> tuple[int, int] | None:
        if depth > 4:
            return None
        position = start
        while position + 8 <= end:
            box_start = position
            box_size = int.from_bytes(data[position : position + 4], "big")
            box_type = data[position + 4 : position + 8]
            header_size = 8
            if box_size == 1:
                if position + 16 > end:
                    return None
                box_size = int.from_bytes(data[position + 8 : position + 16], "big")
                header_size = 16
            elif box_size == 0:
                box_size = end - position
            if box_size < header_size or box_start + box_size > end:
                return None
            payload_start = box_start + header_size
            payload_end = box_start + box_size
            if box_type == b"ispe" and payload_end - payload_start >= 12:
                width = int.from_bytes(data[payload_start + 4 : payload_start + 8], "big")
                height = int.from_bytes(data[payload_start + 8 : payload_start + 12], "big")
                return _positive_dimensions(width, height)
            if box_type in {b"meta", b"iprp", b"ipco", b"moov", b"trak", b"mdia", b"minf", b"dinf", b"stbl"}:
                child_start = payload_start + 4 if box_type == b"meta" else payload_start
                if child_start <= payload_end:
                    dimensions = find_ispe(child_start, payload_end, depth + 1)
                    if dimensions:
                        return dimensions
            position = payload_end
        return None

    return find_ispe(0, len(data))


def image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    """Read dimensions from a bounded image prefix without trusting file names."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[12:16] != b"IHDR" or int.from_bytes(data[8:12], "big") != 13:
            return None
        return _positive_dimensions(*struct.unpack(">II", data[16:24]))
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        if len(data) < 10:
            return None
        return _positive_dimensions(*struct.unpack("<HH", data[6:10]))
    if data[:2] == b"BM":
        if len(data) < 26:
            return None
        dib_size = int.from_bytes(data[14:18], "little")
        if dib_size >= 40 and len(data) >= 26:
            width = int.from_bytes(data[18:22], "little", signed=True)
            height = abs(int.from_bytes(data[22:26], "little", signed=True))
            return _positive_dimensions(width, height)
        if dib_size == 12:
            return _positive_dimensions(
                int.from_bytes(data[18:20], "little"),
                int.from_bytes(data[20:22], "little"),
            )
        return None
    if data[:4] == b"RIFF" and len(data) >= 16 and data[8:12] == b"WEBP":
        chunk_type = data[12:16]
        if chunk_type == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return _positive_dimensions(
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if chunk_type == b"VP8X" and len(data) >= 30:
            return _positive_dimensions(
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if chunk_type == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            width = 1 + (data[21] | (data[22] << 8) | ((data[23] & 0x3F) << 16))
            height = 1 + ((data[23] >> 6) | (data[24] << 2) | ((data[25] & 0x0F) << 10)) if len(data) >= 26 else 0
            return _positive_dimensions(width, height)
    if data[:2] == b"\xff\xd8":
        position = 2
        sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while position + 2 <= len(data):
            if data[position] != 0xFF:
                return None
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                return None
            marker = data[position]
            position += 1
            if marker == 0xDA or marker == 0xD9:
                return None
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(data):
                return None
            if marker in sof_markers:
                if segment_length < 7:
                    return None
                height = int.from_bytes(data[position + 3 : position + 5], "big")
                width = int.from_bytes(data[position + 5 : position + 7], "big")
                return _positive_dimensions(width, height)
            position += segment_length
    if len(data) >= 16 and data[4:8] == b"ftyp":
        return _avif_dimensions(data)
    return None


def _stored_image_dimension(value: Any) -> int | None:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        dimension = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return dimension if dimension > 0 else None


async def probe_news_image_dimensions(client: httpx.AsyncClient, url: str) -> tuple[int, int] | None:
    if not is_allowed_official_image_url(url):
        return None
    max_bytes = 256 * 1024
    try:
        async with client.stream(
            "GET",
            url,
            headers={**NEWS_HEADERS, "Accept": "image/*", "Range": f"bytes=0-{max_bytes - 1}"},
            follow_redirects=False,
        ) as response:
            if response.status_code not in {200, 206}:
                return None
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                remaining = max_bytes - len(content)
                if remaining <= 0:
                    return None
                content.extend(chunk[:remaining])
                dimensions = image_dimensions_from_bytes(bytes(content))
                if dimensions:
                    return dimensions
                if len(content) >= max_bytes:
                    return None
    except (httpx.HTTPError, OSError):
        return None
    return None


async def filter_news_images(client: httpx.AsyncClient, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        source_url = str(image.get("source_url") or "").strip()
        if not is_allowed_official_image_url(source_url):
            continue
        width = _stored_image_dimension(image.get("width"))
        height = _stored_image_dimension(image.get("height"))
        if not news_image_dimensions_allowed(width, height):
            continue
        if width is None or height is None:
            dimensions = await probe_news_image_dimensions(client, str(image.get("source_url") or ""))
            if not dimensions or not news_image_dimensions_allowed(*dimensions):
                continue
            width, height = dimensions
        filtered.append({**image, "source_url": source_url, "width": width, "height": height})
    return filtered


async def fetch_news_page(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    # Validate each redirect before following it, including on manually edited URLs.
    target = validate_news_url(url)
    for _ in range(5):
        for attempt in range(3):
            try:
                async with client.stream("GET", target, headers=headers, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = validate_news_url(urljoin(target, response.headers.get("location", "")))
                        break
                    if response.status_code == 304:
                        return httpx.Response(304, headers=response.headers, request=response.request)
                    if 300 <= response.status_code < 400:
                        raise ValueError("官网重定向响应格式无效")
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    if "html" not in response.headers.get("content-type", "text/html"):
                        raise ValueError("官网未返回 HTML 新闻页面")
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        remaining = 4 * 1024 * 1024 - len(content)
                        if len(chunk) > remaining:
                            raise ValueError("官网页面超过 4 MB 限制")
                        content.extend(chunk)
                    decoded_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key not in {"content-encoding", "content-length"}
                    }
                    return httpx.Response(
                        200, content=bytes(content), headers=decoded_headers, request=response.request
                    )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt == 2:
                    raise
                delay = 2**attempt
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        delay = min(30, max(delay, int(exc.response.headers.get("retry-after", "0"))))
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
        else:
            raise RuntimeError("官网请求重试失败")
    raise ValueError("官网重定向次数过多")
