"""URL helpers for deduplicating equivalent official collaboration pages."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse, urlunparse


_NUMBERED_NEWS = re.compile(r"01_\d+")
_WAYBACK_PATH = re.compile(r"^/web/[^/]+/(https?://.+)$", re.IGNORECASE)


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def wayback_original_url(url: str) -> str:
    """Extract the original URL from a web.archive.org snapshot URL."""
    parsed = urlparse(canonical_url(url))
    if parsed.netloc.lower() != "web.archive.org":
        return canonical_url(url)
    match = _WAYBACK_PATH.match(parsed.path)
    if not match:
        return canonical_url(url)
    original = unquote(match.group(1))
    if parsed.query:
        original = f"{original}?{parsed.query}"
    return canonical_url(original)


def page_identity(url: str) -> str:
    """Return a stable identity for equivalent official and Wayback page URLs."""
    canonical = wayback_original_url(url)
    parsed = urlparse(canonical)
    if parsed.netloc.lower() in {"lovelive-anime.jp", "www.lovelive-anime.jp"}:
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        match = re.fullmatch(r"/news/(01_\d+)\.html", path)
        if match:
            return f"lovelive-news:{match.group(1)}"

        if path == "/nijigasaki/detail.php":
            value = query.get("p", [""])[0]
            if _NUMBERED_NEWS.fullmatch(value):
                return f"lovelive-news:{value}"

    return f"url:{canonical}"


def dedupe_page_urls(urls: list[str]) -> list[str]:
    """Keep the first URL for each page identity while preserving input order."""
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        canonical = canonical_url(url)
        identity = page_identity(canonical)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(canonical)
    return result
