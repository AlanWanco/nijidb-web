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
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from .collaboration_urls import canonical_url, dedupe_page_urls, page_identity
except ImportError:  # pragma: no cover - supports running this file directly
    from collaboration_urls import canonical_url, dedupe_page_urls, page_identity

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "frontend/src/content/collaborationIllustrations.json"
IMAGE_ROOT = ROOT / "data/images/illustrations"
PAGE_CACHE_ROOT = ROOT / "data/illustration-page-cache"
MANIFEST_PATH = IMAGE_ROOT / "manifest.json"
USER_AGENT = "nijidb-collaboration-illustration-collector/1.0"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
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


def image_dimensions(data: bytes) -> tuple[int, int]:
    if data[:2] == b"BM" and len(data) >= 26:
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = abs(int.from_bytes(data[22:26], "little", signed=True))
        return width, height
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:3] == b"GIF" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if data[12:16] == b"VP8 " and len(data) >= 30:
            marker = data.find(b"\x9d\x01\x2a", 20, 40)
            if marker >= 0 and len(data) >= marker + 7:
                return int.from_bytes(data[marker + 3:marker + 5], "little") & 0x3FFF, int.from_bytes(
                    data[marker + 5:marker + 7], "little"
                ) & 0x3FFF
        if data[12:16] == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(
                range(0xCD, 0xD0)
            ):
                if offset + 7 <= len(data):
                    return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(
                        data[offset + 3:offset + 5], "big"
                    )
                break
            offset += length
    return 0, 0


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
        if not path.exists():
            continue
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if cached.get("identity") == page_identity(url):
            return cached
        if cached.get("url") == canonical_url(url):
            return cached
        if cached.get("url") and page_identity(cached["url"]) == page_identity(url):
            return cached
    return None


def write_page_cache(url: str, payload: dict[str, Any]) -> None:
    PAGE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    path = PAGE_CACHE_ROOT / f"{cache_key(url)}.json"
    path.write_text(
        json.dumps(
            {"url": canonical_url(url), "identity": page_identity(url), **payload},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def x_status_id(url: str) -> str | None:
    match = re.search(r"/(?:status|statuses)/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


async def fetch_x_media(client: httpx.AsyncClient, page_url: str) -> list[dict[str, Any]]:
    status_id = x_status_id(page_url)
    if not status_id:
        return []
    try:
        response = await client.get(f"https://api.fxtwitter.com/status/{status_id}", timeout=25)
        if not response.is_success:
            return []
        tweet = response.json().get("tweet", {})
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
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, AttributeError):
        return []


async def fetch_page(client: httpx.AsyncClient, url: str, item: dict[str, Any], refresh: bool) -> dict[str, Any]:
    if not refresh:
        cached = read_page_cache(url)
        if cached:
            return cached
    try:
        response = await client.get(url, follow_redirects=True, timeout=25)
        content_type = response.headers.get("content-type", "")
        if "image/" in content_type:
            payload = {
                "status": response.status_code,
                "final_url": str(response.url),
                "content_type": content_type,
                "page_title": "",
                "candidates": [{"url": canonical_url(str(response.url)), "score": 60, "kind": "direct", "context": ""}],
                "error": "" if response.is_success else f"HTTP {response.status_code}",
            }
        elif response.is_success and "html" in content_type:
            final_url = str(response.url)
            page_title, candidates = extract_page_candidates(response.content, final_url, item)
            x_candidates = await fetch_x_media(client, final_url)
            known_urls = {candidate["url"] for candidate in candidates}
            candidates.extend(candidate for candidate in x_candidates if candidate["url"] not in known_urls)
            candidates.sort(key=lambda value: (-value["score"], value["url"]))
            payload = {
                "status": response.status_code,
                "final_url": final_url,
                "content_type": content_type,
                "page_title": page_title,
                "candidates": candidates,
                "error": "",
            }
        else:
            payload = {
                "status": response.status_code,
                "final_url": str(response.url),
                "content_type": content_type,
                "page_title": "",
                "candidates": [],
                "error": f"HTTP {response.status_code}",
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
    async with httpx.AsyncClient(headers=headers, limits=limits) as client:
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
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
        content_type = response.headers.get("content-type", "")
        data = response.content
        effective_content_type = detected_content_type(data, content_type, url)
        if not response.is_success or not data or "image/" not in effective_content_type.lower():
            return {"ok": False, "error": f"HTTP {response.status_code} {content_type}"}
        if len(data) < MIN_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
            return {"ok": False, "error": f"size {len(data)}"}
        width, height = image_dimensions(data)
        if width and height and min(width, height) < MIN_IMAGE_SIDE:
            return {"ok": False, "error": f"dimensions {width}x{height}"}
        digest = hashlib.sha256(data).hexdigest()
        return {
            "ok": True,
            "url": url,
            "final_url": canonical_url(str(response.url)),
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
    if not MANIFEST_PATH.exists():
        return {"schema_version": 1, "generated_at": "", "items": {}, "assets": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema_version": 1, "generated_at": "", "items": {}, "assets": {}}


def write_manifest(manifest: dict[str, Any]) -> None:
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def local_asset_exists(image: dict[str, Any]) -> bool:
    path = image.get("path", "")
    prefix = "/media/illustrations/"
    if not path.startswith(prefix):
        return False
    return (IMAGE_ROOT / path.removeprefix(prefix)).is_file()


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

    async with httpx.AsyncClient(headers=headers, limits=limits) as client:
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
                if not absolute_path.exists():
                    absolute_path.parent.mkdir(parents=True, exist_ok=True)
                    absolute_path.write_bytes(result["data"])
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
