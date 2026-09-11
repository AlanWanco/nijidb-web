"""Bounded official-news fetching; no arbitrary URL proxy or private-network requests."""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlparse

import httpx

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
