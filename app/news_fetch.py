"""Bounded official-news fetching; no arbitrary URL proxy or private-network requests."""

from __future__ import annotations

import asyncio
import struct
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.news import news_image_dimensions_allowed

OFFICIAL_HOSTS = {"www.lovelive-anime.jp", "lovelive-anime.jp", "lovelive-as.bushimo.jp"}
NEWS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en;q=0.8",
    "Referer": "https://www.lovelive-anime.jp/nijigasaki/topics.php",
}


def validate_news_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in OFFICIAL_HOSTS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and "\\" not in url
        )
    except ValueError:
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


def image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:3] == b"GIF" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"BM" and len(data) >= 26:
        return struct.unpack("<II", data[18:26])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if data[12:16] == b"VP8X" and len(data) >= 30:
            return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
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
            if position + 2 > len(data):
                break
            segment_length = int.from_bytes(data[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(data):
                break
            if marker in sof_markers and segment_length >= 7:
                height = int.from_bytes(data[position + 3 : position + 5], "big")
                width = int.from_bytes(data[position + 5 : position + 7], "big")
                return width, height
            position += segment_length
    return None


def _stored_image_dimension(value: Any) -> int | None:
    try:
        dimension = int(value or 0)
    except (TypeError, ValueError):
        return None
    return dimension if dimension > 0 else None


async def probe_news_image_dimensions(client: httpx.AsyncClient, url: str) -> tuple[int, int] | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
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
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
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
        width = _stored_image_dimension(image.get("width"))
        height = _stored_image_dimension(image.get("height"))
        if not news_image_dimensions_allowed(width, height):
            continue
        if width is None or height is None:
            dimensions = await probe_news_image_dimensions(client, str(image.get("source_url") or ""))
            if not dimensions or not news_image_dimensions_allowed(*dimensions):
                continue
            width, height = dimensions
        filtered.append({**image, "width": width, "height": height})
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
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    if "html" not in response.headers.get("content-type", "text/html"):
                        raise ValueError("官网未返回 HTML 新闻页面")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise ValueError("官网页面超过 4 MB 限制")
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
