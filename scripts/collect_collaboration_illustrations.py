#!/usr/bin/env python3
"""Collect locally cached illustration candidates from the indexed official pages.

The source index is metadata only. This collector deliberately keeps the source URL
and page URL for every downloaded image, filters common site chrome, and writes a
resumable local manifest under data/images/illustrations/.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import ipaddress
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpcore
import httpx
from bs4 import BeautifulSoup
from httpcore._backends.auto import AutoBackend
from httpcore._backends.sync import SyncBackend

try:
    from .collaboration_urls import canonical_url, dedupe_page_urls, page_identity
except ImportError:  # pragma: no cover - supports running this file directly
    from collaboration_urls import canonical_url, dedupe_page_urls, page_identity

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.news_fetch import image_dimensions_from_bytes  # noqa: E402

INDEX_PATH = ROOT / "frontend/src/content/collaborationIllustrations.json"
IMAGE_ROOT = ROOT / "data/images/illustrations"
PAGE_CACHE_ROOT = ROOT / "data/illustration-page-cache"
MANIFEST_PATH = IMAGE_ROOT / "manifest.json"
USER_AGENT = "nijidb-collaboration-illustration-collector/1.0"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MIN_IMAGE_BYTES = 8 * 1024
MIN_IMAGE_SIDE = 160

BAD_PATH_PARTS = {
    "arrow",
    "background",
    "bg",
    "button",
    "close",
    "common",
    "favicon",
    "footer",
    "header",
    "icon",
    "ico",
    "livelogo",
    "loading",
    "sifbanner",
    "logo",
    "menu",
    "nav",
    "pixel",
    "qr",
    "search",
    "share",
    "sns",
    "sprite",
    "tracking",
}
GOOD_PATH_PARTS = {
    "art",
    "goods",
    "illust",
    "illustration",
    "image",
    "keyvisual",
    "kv",
    "main",
    "visual",
}
IMAGE_EXTENSIONS = {".avif", ".bmp", ".jpeg", ".jpg", ".png", ".webp"}
GENERIC_ASSET_MARKERS = ("カフェkvs", "cafe_kv", "cafe-kv", "cafe kv")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cache_key(url: str) -> str:
    return hashlib.sha256(page_identity(url).encode()).hexdigest()


def legacy_cache_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def clean_text(value: str) -> str:
    return " ".join(value.split())


def item_keywords(item: dict[str, Any]) -> list[str]:
    values = [item["title"], *item["collaboration"]]
    keywords: list[str] = []
    for value in values:
        for part in re.split(r"[\n（）()「」『』【】/／・×,，:：]+", value):
            part = clean_text(part)
            if len(part) >= 3 and part not in keywords:
                keywords.append(part)
    return keywords


def public_addresses(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OSError("目标主机解析失败") from exc
    addresses: list[str] = []
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except (IndexError, ValueError):
            raise OSError("目标主机解析结果无效") from None
        if not address.is_global:
            raise OSError("目标主机解析到非公网地址")
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise OSError("目标主机没有公网地址")
    return addresses


class PublicOnlyNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and connect to the same globally routable address set."""

    def __init__(self) -> None:
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        last_error: Exception | None = None
        for address in public_addresses(host, port):
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (OSError, httpcore.ConnectError) as exc:
                last_error = exc
        raise OSError("无法连接到目标公网地址") from last_error


class PublicOnlySyncNetworkBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self._backend = SyncBackend()

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address=None, socket_options=None
    ):
        last_error: Exception | None = None
        for address in public_addresses(host, port):
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (OSError, httpcore.ConnectError) as exc:
                last_error = exc
        raise OSError("无法连接到目标公网地址") from last_error


class PublicOnlySyncTransport(httpx.HTTPTransport):
    def __init__(self, limits: httpx.Limits) -> None:
        super().__init__(trust_env=False, limits=limits)
        old_pool = self._pool
        self._pool = httpcore.ConnectionPool(
            ssl_context=old_pool._ssl_context,
            max_connections=old_pool._max_connections,
            max_keepalive_connections=old_pool._max_keepalive_connections,
            keepalive_expiry=old_pool._keepalive_expiry,
            http1=old_pool._http1,
            http2=old_pool._http2,
            retries=old_pool._retries,
            local_address=old_pool._local_address,
            uds=old_pool._uds,
            socket_options=old_pool._socket_options,
            network_backend=PublicOnlySyncNetworkBackend(),
        )


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    def __init__(self, limits: httpx.Limits) -> None:
        super().__init__(trust_env=False, limits=limits)
        old_pool = self._pool
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=old_pool._ssl_context,
            max_connections=old_pool._max_connections,
            max_keepalive_connections=old_pool._max_keepalive_connections,
            keepalive_expiry=old_pool._keepalive_expiry,
            http1=old_pool._http1,
            http2=old_pool._http2,
            retries=old_pool._retries,
            local_address=old_pool._local_address,
            uds=old_pool._uds,
            socket_options=old_pool._socket_options,
            network_backend=PublicOnlyNetworkBackend(),
        )


def public_fetch_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
        decoded_path = unquote(parsed.path)
    except (TypeError, ValueError):
        return False
    host = parsed.hostname or ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or any(character.isspace() for character in host)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "#" in url
        or ("?" in url and not parsed.query)
        or "\\" in url
        or "?" in decoded_path
        or "#" in decoded_path
        or "\\" in decoded_path
        or any(part in {".", ".."} for part in decoded_path.split("/") if part)
        or parsed.netloc.endswith(":")
        or port == 0
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        or host.lower() in {"localhost", "localhost.localdomain"}
        or host.lower().endswith((".local", ".internal"))
    ):
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        records = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0]))
        except (IndexError, ValueError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


def is_image_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/profile_images/" in path or parsed.netloc.lower() == "abs.twimg.com":
        return False
    return Path(path).suffix in IMAGE_EXTENSIONS or any(
        token in path for token in ("/image", "/img", "/photo", "/picture", "ogp")
    )


def is_site_chrome_url(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    if any(marker in path for marker in GENERIC_ASSET_MARKERS):
        return True
    tokens = set(re.split(r"[/_.-]+", path))
    if tokens & BAD_PATH_PARTS or any(
        token.startswith(("icon", "ico", "logo", "favicon")) for token in tokens
    ):
        return True
    return (
        path.endswith("/images/ogp.png")
        or path.endswith("/img/ogp.png")
        or "/llss_banner" in path
        or "/h1s." in path
        or path.endswith("/store.png")
    )


def path_score(url: str) -> tuple[int, bool]:
    path = urlparse(url).path.lower()
    tokens = set(re.split(r"[/_.-]+", path))
    bad = tokens & BAD_PATH_PARTS
    good = tokens & GOOD_PATH_PARTS
    score = len(good) * 5 - len(bad) * 15
    if "thumbnail" in path or "thumb" in path or re.search(r"[-_]\d{2,4}x\d{2,4}", path):
        score -= 12
    if "ogp" in path:
        score -= 8
    return score, bool(bad)


def detected_content_type(data: bytes, content_type: str, url: str) -> str:
    lowered = content_type.lower().split(";", 1)[0]
    if lowered.startswith("image/"):
        return content_type
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:2] == b"BM":
        return "image/bmp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return content_type


def image_extension(data: bytes, content_type: str, url: str) -> str:
    content_type = detected_content_type(data, content_type, url)
    lowered = content_type.lower().split(";", 1)[0]
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/bmp": ".bmp",
    }
    if lowered in mapping:
        return mapping[lowered]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".bin"


def candidate_context(node: Any) -> str:
    parent = node.parent if getattr(node, "parent", None) else node
    parts = [node.get("alt", ""), node.get("title", "")]
    for _ in range(3):
        if not parent:
            break
        parts.append(parent.get_text(" ", strip=True)[:600])
        parent = parent.parent
    return clean_text(" ".join(parts))


def candidate_score(url: str, kind: str, node: Any, keywords: list[str]) -> tuple[int, str]:
    score = {
        "og": 48,
        "twitter": 42,
        "jsonld": 40,
        "img": 26,
        "source": 23,
        "link": 18,
        "style": 14,
        "x_media": 76,
    }.get(kind, 10)
    path_bonus, has_bad_path = path_score(url)
    score += path_bonus
    context = candidate_context(node) if node is not None and hasattr(node, "get") else ""
    haystack = f"{url} {context}".lower()
    for keyword in keywords:
        if keyword.lower() in haystack:
            score += min(18, max(5, len(keyword) // 3))
            break
    ancestor = node
    for _ in range(5):
        if not ancestor:
            break
        name = getattr(ancestor, "name", "") or ""
        classes = " ".join(ancestor.get("class", [])) if hasattr(ancestor, "get") else ""
        marker = f"{name} {classes}".lower()
        if name in {"main", "article", "figure"} or any(
            token in marker for token in ("content", "detail", "news", "article", "visual", "goods", "product")
        ):
            score += 12
        ancestor = getattr(ancestor, "parent", None)
    if has_bad_path:
        score -= 20
    return score, context[:300]


def parse_srcset(value: str) -> list[str]:
    urls = []
    for part in value.split(","):
        candidate = part.strip().split(" ", 1)[0]
        if candidate:
            urls.append(candidate)
    return urls


def extract_page_candidates(content: bytes, page_url: str, item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    soup = BeautifulSoup(content, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    keywords = item_keywords(item)
    found: dict[str, dict[str, Any]] = {}

    def add(raw_url: str | None, kind: str, node: Any = None) -> None:
        if not raw_url or raw_url.startswith(("data:", "javascript:", "mailto:")):
            return
        absolute = canonical_url(urljoin(page_url, raw_url.strip()))
        if (
            urlparse(absolute).scheme not in {"http", "https"}
            or not is_image_url(absolute)
            or is_site_chrome_url(absolute)
        ):
            return
        score, context = candidate_score(absolute, kind, node, keywords)
        current = found.get(absolute)
        candidate = {
            "url": absolute,
            "score": score,
            "kind": kind,
            "context": context,
        }
        if current is None or candidate["score"] > current["score"]:
            found[absolute] = candidate

    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"], meta[name="twitter:image:src"]'):
        kind = "og" if meta.get("property") == "og:image" else "twitter"
        add(meta.get("content"), kind, meta)
    for image in soup.select("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-image", "data-fancybox"):
            add(image.get(attr), "img", image)
        for attr in ("srcset", "data-srcset"):
            for value in parse_srcset(image.get(attr, "")):
                add(value, "img", image)
    for source in soup.select("source[srcset], source[data-srcset]"):
        for value in parse_srcset(source.get("srcset") or source.get("data-srcset") or ""):
            add(value, "source", source)
    for anchor in soup.select("a[href]"):
        if is_image_url(urljoin(page_url, anchor.get("href", ""))):
            add(anchor.get("href"), "link", anchor)
    for element in soup.select("[style*='url(']"):
        for raw in re.findall(r"url\(['\"]?([^)'\"]+)", element.get("style", "")):
            add(raw, "style", element)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue

        def visit(value: Any) -> None:
            if isinstance(value, str):
                if is_image_url(urljoin(page_url, value)):
                    add(value, "jsonld", script)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() == "image":
                        visit(child)

        visit(payload)

    return page_title, sorted(found.values(), key=lambda value: (-value["score"], value["url"]))


def load_items() -> list[dict[str, Any]]:
    payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return [item for year in payload["years"] for item in year["items"]]


def read_page_cache(url: str) -> dict[str, Any] | None:
    paths = [PAGE_CACHE_ROOT / f"{cache_key(url)}.json"]
    legacy_path = PAGE_CACHE_ROOT / f"{legacy_cache_key(url)}.json"
    if legacy_path not in paths:
        paths.append(legacy_path)
    for path in paths:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PAGE_BYTES:
                continue
            cached = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        except json.JSONDecodeError:
            continue
        if not isinstance(cached, dict):
            continue
        if cached.get("identity") == page_identity(url):
            return cached
        if cached.get("url") == canonical_url(url):
            return cached
        if cached.get("url") and page_identity(cached["url"]) == page_identity(url):
            return cached
    return None


def write_page_cache(url: str, payload: dict[str, Any]) -> None:
    if PAGE_CACHE_ROOT.is_symlink():
        raise RuntimeError("页面缓存目录不能是符号链接")
    PAGE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    path = PAGE_CACHE_ROOT / f"{cache_key(url)}.json"
    content = (
        json.dumps(
            {"url": canonical_url(url), "identity": page_identity(url), **payload},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if len(content.encode("utf-8")) > MAX_PAGE_BYTES:
        raise RuntimeError("页面缓存超过 8 MB 限制")
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=PAGE_CACHE_ROOT)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def x_status_id(url: str) -> str | None:
    match = re.search(r"/(?:status|statuses)/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


async def fetch_x_media(client: httpx.AsyncClient, page_url: str) -> list[dict[str, Any]]:
    status_id = x_status_id(page_url)
    if not status_id:
        return []
    try:
        status, _, _, content = await bounded_get(
            client,
            f"https://api.fxtwitter.com/status/{status_id}",
            2 * 1024 * 1024,
        )
        if not 200 <= status < 300:
            return []
        tweet = json.loads(content.decode("utf-8")).get("tweet", {})
        context = clean_text(tweet.get("text", ""))
        candidates = []
        for media in tweet.get("media", {}).get("photos", []):
            image_url = media.get("url")
            if image_url:
                candidates.append(
                    {
                        "url": canonical_url(image_url),
                        "score": 76,
                        "kind": "x_media",
                        "context": context[:300],
                    }
                )
        return candidates
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, AttributeError):
        return []


async def bounded_get(
    client: httpx.AsyncClient, url: str, maximum: int, *, timeout: float = 30
) -> tuple[int, dict[str, str], str, bytes]:
    async with client.stream("GET", url, follow_redirects=False, timeout=timeout) as response:
        try:
            announced_length = int(response.headers.get("content-length", "0") or 0)
        except (TypeError, ValueError, OverflowError):
            announced_length = 0
        if announced_length > maximum:
            raise ValueError(f"response exceeds {maximum} bytes")
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(content) + len(chunk) > maximum:
                raise ValueError(f"response exceeds {maximum} bytes")
            content.extend(chunk)
        return response.status_code, dict(response.headers), str(response.url), bytes(content)


async def fetch_page(client: httpx.AsyncClient, url: str, item: dict[str, Any], refresh: bool) -> dict[str, Any]:
    if not public_fetch_url(url):
        return {
            "url": canonical_url(url),
            "status": 0,
            "content_type": "",
            "page_title": "",
            "candidates": [],
            "error": "blocked URL",
        }
    if not refresh:
        cached = read_page_cache(url)
        if cached:
            return cached
    try:
        status_code, response_headers, final_url, content = await bounded_get(client, url, MAX_PAGE_BYTES)
        content_type = response_headers.get("content-type", "")
        if "image/" in content_type:
            payload = {
                "status": status_code,
                "final_url": final_url,
                "content_type": content_type,
                "page_title": "",
                "candidates": [{"url": canonical_url(final_url), "score": 60, "kind": "direct", "context": ""}],
                "error": "" if 200 <= status_code < 300 else f"HTTP {status_code}",
            }
        elif 200 <= status_code < 300 and "html" in content_type:
            page_title, candidates = extract_page_candidates(content, final_url, item)
            x_candidates = await fetch_x_media(client, final_url)
            known_urls = {candidate["url"] for candidate in candidates}
            candidates.extend(candidate for candidate in x_candidates if candidate["url"] not in known_urls)
            candidates.sort(key=lambda value: (-value["score"], value["url"]))
            payload = {
                "status": status_code,
                "final_url": final_url,
                "content_type": content_type,
                "page_title": page_title,
                "candidates": candidates,
                "error": "",
            }
        else:
            payload = {
                "status": status_code,
                "final_url": final_url,
                "content_type": content_type,
                "page_title": "",
                "candidates": [],
                "error": f"HTTP {status_code}",
            }
    except Exception as exc:  # network failures are recorded per source page
        payload = {
            "status": 0,
            "final_url": url,
            "content_type": "",
            "page_title": "",
            "candidates": [],
            "error": str(exc),
        }
    write_page_cache(url, payload)
    return {"url": canonical_url(url), **payload}


def page_quality(page: dict[str, Any]) -> int:
    status = int(page.get("status") or 0)
    if 200 <= status < 400 and page.get("candidates"):
        return 2
    if 200 <= status < 400:
        return 1
    return 0


async def fetch_all_pages(items: list[dict[str, Any]], refresh: bool, concurrency: int) -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    page_groups: dict[str, dict[str, Any]] = {}
    for item in items:
        for link in item["official_links"]:
            url = canonical_url(link["url"])
            identity = page_identity(url)
            group = page_groups.setdefault(identity, {"urls": [], "item": item})
            if url not in group["urls"]:
                group["urls"].append(url)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        headers=headers,
        limits=limits,
        trust_env=False,
        follow_redirects=False,
        transport=PublicOnlyTransport(limits),
    ) as client:
        async def fetch_one(identity: str, group: dict[str, Any], index: int) -> None:
            best: dict[str, Any] | None = None
            async with semaphore:
                for url in group["urls"]:
                    page = await fetch_page(client, url, group["item"], refresh)
                    if best is None or page_quality(page) > page_quality(best):
                        best = page
                    # Equivalent aliases normally contain the same content. Only try
                    # another alias when the first one failed or yielded no candidates.
                    if page_quality(page) >= 2:
                        break
            if best is None:
                return
            for url in group["urls"]:
                pages[url] = best
            print(f"页面 {index}/{len(page_groups)}: {best['status']} {identity}")

        await asyncio.gather(
            *(fetch_one(identity, group, index) for index, (identity, group) in enumerate(page_groups.items(), 1))
        )
    return pages


async def download_image(client: httpx.AsyncClient, candidate: dict[str, Any]) -> dict[str, Any]:
    url = candidate["url"]
    if not public_fetch_url(url):
        return {"ok": False, "error": "blocked URL"}
    try:
        async with client.stream("GET", url, follow_redirects=False, timeout=30) as response:
            content_type = response.headers.get("content-type", "")
            try:
                announced_length = int(response.headers.get("content-length", "0") or 0)
            except (TypeError, ValueError, OverflowError):
                announced_length = 0
            if announced_length > MAX_IMAGE_BYTES:
                return {"ok": False, "error": f"size {announced_length}"}
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(content) + len(chunk) > MAX_IMAGE_BYTES:
                    return {"ok": False, "error": f"size > {MAX_IMAGE_BYTES}"}
                content.extend(chunk)
            data = bytes(content)
            status_code = response.status_code
            final_url = str(response.url)
        effective_content_type = detected_content_type(data, content_type, url)
        if not 200 <= status_code < 300 or not data or "image/" not in effective_content_type.lower():
            return {"ok": False, "error": f"HTTP {status_code} {content_type}"}
        if len(data) < MIN_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
            return {"ok": False, "error": f"size {len(data)}"}
        dimensions = image_dimensions_from_bytes(data)
        if not dimensions:
            return {"ok": False, "error": "invalid dimensions"}
        width, height = dimensions
        if min(width, height) < MIN_IMAGE_SIDE or max(width, height) > 10000:
            return {"ok": False, "error": f"dimensions {width}x{height}"}
        digest = hashlib.sha256(data).hexdigest()
        return {
            "ok": True,
            "url": url,
            "final_url": canonical_url(final_url),
            "content_type": effective_content_type,
            "data": data,
            "sha256": digest,
            "width": width,
            "height": height,
            "bytes": len(data),
            "extension": image_extension(data, content_type, url),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def load_existing_manifest() -> dict[str, Any]:
    try:
        metadata = MANIFEST_PATH.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PAGE_BYTES:
            return {"schema_version": 1, "generated_at": "", "items": {}, "assets": {}}
        payload = json.loads(MANIFEST_PATH.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema_version": 1, "generated_at": "", "items": {}, "assets": {}}
    return payload if isinstance(payload, dict) else {"schema_version": 1, "generated_at": "", "items": {}, "assets": {}}


def write_manifest(manifest: dict[str, Any]) -> None:
    if IMAGE_ROOT.is_symlink() or MANIFEST_PATH.is_symlink():
        raise RuntimeError("插画目录或清单不能是符号链接")
    directory_fd = _open_asset_directory((), create=True)
    temporary_name = f".{MANIFEST_PATH.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, MANIFEST_PATH.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _open_asset_directory(parts: tuple[str, ...], *, create: bool) -> int:
    if IMAGE_ROOT.is_symlink():
        raise RuntimeError("插画保存目录不能是符号链接")
    if create:
        IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(IMAGE_ROOT, flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _asset_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, MAX_IMAGE_BYTES + 1 - total))
        if not chunk:
            return digest.hexdigest()
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            return ""
        digest.update(chunk)


def publish_asset(path: Path, data: bytes, expected_digest: str) -> None:
    try:
        relative = path.relative_to(IMAGE_ROOT)
    except ValueError as exc:
        raise RuntimeError("插画路径不在允许的目录内") from exc
    parts = tuple(relative.parts)
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise RuntimeError("插画路径格式无效")
    directory_fd = _open_asset_directory(parts[:-1], create=True)
    temporary_name = f".{parts[-1]}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        try:
            existing_fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError:
            existing_fd = -1
        if existing_fd >= 0:
            try:
                metadata = os.fstat(existing_fd)
                if not stat.S_ISREG(metadata.st_mode) or _asset_digest(existing_fd) != expected_digest:
                    raise RuntimeError(f"已有插画文件内容与摘要不匹配：{path}")
            finally:
                os.close(existing_fd)
            return
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("插画文件写入失败")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
        try:
            os.link(temporary_name, parts[-1], src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except FileExistsError:
            existing_fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode) or _asset_digest(existing_fd) != expected_digest:
                    raise RuntimeError(f"已有插画文件内容与摘要不匹配：{path}")
            finally:
                os.close(existing_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def local_asset_exists(image: dict[str, Any]) -> bool:
    path = image.get("path", "")
    prefix = "/media/illustrations/"
    if not path.startswith(prefix):
        return False
    if IMAGE_ROOT.is_symlink():
        return False
    relative = Path(path.removeprefix(prefix))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    try:
        metadata = (IMAGE_ROOT / relative).lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode)


async def collect_images(
    items: list[dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    max_per_item: int,
    candidate_limit: int,
    concurrency: int,
) -> dict[str, Any]:
    manifest = load_existing_manifest()
    manifest.update(
        {
            "schema_version": 1,
            "generated_at": now_iso(),
            "source_index": "frontend/src/content/collaborationIllustrations.json",
            "storage_root": "data/images/illustrations",
            "public_root": "/media/illustrations",
        }
    )
    manifest.setdefault("items", {})
    manifest.setdefault("assets", {})
    asset_lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        headers=headers,
        limits=limits,
        trust_env=False,
        follow_redirects=False,
        transport=PublicOnlyTransport(limits),
    ) as client:
        async def collect_one(item: dict[str, Any], index: int) -> None:
            page_urls = dedupe_page_urls([link["url"] for link in item["official_links"]])
            candidates: dict[str, dict[str, Any]] = {}
            for page_url in page_urls:
                page = pages.get(page_url, {})
                for candidate in page.get("candidates", []):
                    if is_site_chrome_url(candidate.get("url", "")):
                        continue
                    candidate = {**candidate, "source_page": page.get("final_url") or page_url}
                    existing = candidates.get(candidate["url"])
                    if existing is None or candidate["score"] > existing["score"]:
                        candidates[candidate["url"]] = candidate
            ordered = sorted(candidates.values(), key=lambda value: (-value["score"], value["url"]))[:candidate_limit]
            previous = manifest["items"].get(item["id"], {})
            downloaded = [
                image
                for image in previous.get("images", [])
                if not is_site_chrome_url(image.get("source_url", "")) and local_asset_exists(image)
            ]
            if max_per_item > 0:
                downloaded = downloaded[:max_per_item]
            previous_urls = {image.get("source_url") for image in downloaded}
            previous_hashes = {image.get("sha256") for image in downloaded}
            failures: list[dict[str, str]] = []
            for candidate in ordered:
                if max_per_item > 0 and len(downloaded) >= max_per_item:
                    break
                if candidate["url"] in previous_urls:
                    continue
                if candidate["score"] < 20:
                    continue
                async with semaphore:
                    result = await download_image(client, candidate)
                if not result["ok"]:
                    failures.append({"url": candidate["url"], "error": result["error"]})
                    continue
                if result["sha256"] in previous_hashes:
                    continue
                asset_path = f"assets/{result['sha256']}{result['extension']}"
                absolute_path = IMAGE_ROOT / asset_path
                publish_asset(absolute_path, result["data"], result["sha256"])
                async with asset_lock:
                    manifest["assets"].setdefault(
                        result["sha256"],
                        {
                            "path": f"/media/illustrations/{asset_path}",
                            "width": result["width"],
                            "height": result["height"],
                            "bytes": result["bytes"],
                        },
                    )
                previous_hashes.add(result["sha256"])
                previous_urls.add(candidate["url"])
                downloaded.append(
                    {
                        "path": f"/media/illustrations/{asset_path}",
                        "source_url": candidate["url"],
                        "source_page": candidate["source_page"],
                        "sha256": result["sha256"],
                        "width": result["width"],
                        "height": result["height"],
                        "bytes": result["bytes"],
                        "score": candidate["score"],
                        "kind": candidate["kind"],
                        "context": candidate.get("context", ""),
                    }
                )
                if max_per_item > 0 and len(downloaded) >= max_per_item:
                    break
            status = "complete" if downloaded else "unavailable"
            if max_per_item > 0 and downloaded and len(downloaded) < max_per_item:
                status = "partial"
            manifest["items"][item["id"]] = {
                "first_seen": item["first_seen"],
                "title": item["title"],
                "images": downloaded,
                "status": status,
                "attempted_pages": page_urls,
                "candidate_count": len(candidates),
                "failures": failures[:20],
            }
            print(
                f"图片 {index}/{len(items)}: {len(downloaded)}/"
                f"{max_per_item if max_per_item > 0 else '全部'} "
                f"{status} {item['title'].splitlines()[0]}"
            )

        await asyncio.gather(*(collect_one(item, index) for index, item in enumerate(items, 1)))
    write_manifest(manifest)
    return manifest


def summarize(manifest: dict[str, Any], items: list[dict[str, Any]]) -> None:
    item_map = manifest.get("items", {})
    statuses = Counter(item_map.get(item["id"], {}).get("status", "missing") for item in items)
    image_count = sum(len(item_map.get(item["id"], {}).get("images", [])) for item in items)
    print("\n采集完成")
    print("项目状态：", dict(statuses))
    print("已关联图片：", image_count)
    print("去重资源：", len(manifest.get("assets", {})))
    print("清单：", MANIFEST_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-item", type=int, default=0, help="每条记录最多图片数；0 表示不限制")
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 项，便于试跑")
    parser.add_argument("--refresh-pages", action="store_true")
    args = parser.parse_args()
    items = load_items()
    if args.limit:
        items = items[:args.limit]
    pages = asyncio.run(fetch_all_pages(items, args.refresh_pages, args.concurrency))
    manifest = asyncio.run(collect_images(items, pages, args.max_per_item, args.candidate_limit, args.concurrency))
    summarize(manifest, items)


if __name__ == "__main__":
    main()
