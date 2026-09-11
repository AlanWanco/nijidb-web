#!/usr/bin/env python3
"""Recover illustration candidates from archived official Love Live pages."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

import collect_collaboration_illustrations as collector

ROOT = Path(__file__).resolve().parents[1]
WAYBACK_ROOT = ROOT / "data/illustration-wayback-cache"
WAYBACK_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"
USER_AGENT = "nijidb-collaboration-illustration-collector/1.0"


def canonical(url: str) -> str:
    parsed = urlparse(collector.canonical_url(url))
    host = parsed.netloc.lower()
    if host == "lovelive-anime.jp":
        host = "www.lovelive-anime.jp"
    scheme = "https" if host == "www.lovelive-anime.jp" else parsed.scheme
    return urlunparse((scheme, host, parsed.path, parsed.params, parsed.query, ""))


def is_lovelive_page(url: str) -> bool:
    return urlparse(url).netloc == "www.lovelive-anime.jp"


def item_date(item: dict[str, Any]) -> date:
    return date.fromisoformat(item["first_seen"])


def capture_cache_path(prefix: str, match_type: str) -> Path:
    return WAYBACK_ROOT / f"captures-{match_type}-{collector.cache_key(prefix)}.json"


def load_capture_cache(prefix: str, match_type: str) -> list[dict[str, str]] | None:
    path = capture_cache_path(prefix, match_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_capture_cache(prefix: str, match_type: str, captures: list[dict[str, str]]) -> None:
    WAYBACK_ROOT.mkdir(parents=True, exist_ok=True)
    capture_cache_path(prefix, match_type).write_text(
        json.dumps(captures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_cdx(content: bytes) -> list[dict[str, str]]:
    payload = json.loads(content)
    if not payload:
        return []
    headers = payload[0]
    return [dict(zip(headers, row)) for row in payload[1:]]


async def fetch_cdx(
    client: httpx.AsyncClient,
    prefix: str,
    match_type: str = "prefix",
    refresh: bool = False,
    collapse: str | None = "urlkey",
) -> list[dict[str, str]]:
    cache_type = match_type if collapse is not None else f"{match_type}-all"
    if not refresh:
        cached = load_capture_cache(prefix, cache_type)
        if cached is not None:
            return cached
    params = {
        "url": prefix,
        "matchType": match_type,
        "output": "json",
        "filter": "statuscode:200",
        "fl": "timestamp,original,mimetype,statuscode",
        "limit": "10000",
    }
    if collapse is not None:
        params["collapse"] = collapse
    for attempt in range(5):
        try:
            response = await client.get(WAYBACK_API, params=params, timeout=180)
            if response.status_code == 200:
                captures = parse_cdx(response.content)
                save_capture_cache(prefix, cache_type, captures)
                return captures
        except (httpx.HTTPError, json.JSONDecodeError):
            pass
        await asyncio.sleep(6 * (attempt + 1))
    save_capture_cache(prefix, cache_type, [])
    return []


def archive_url(timestamp: str, original: str) -> str:
    return f"{WAYBACK_BASE}/{timestamp}id_/{original}"


def choose_capture(captures: list[dict[str, str]], target: date) -> dict[str, str] | None:
    valid = []
    for capture in captures:
        timestamp = capture.get("timestamp", "")
        if not re.fullmatch(r"\d{14}", timestamp):
            continue
        try:
            captured = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError:
            continue
        valid.append((abs((captured - target).days), captured > target, capture))
    if not valid:
        return None
    valid.sort(key=lambda value: (value[0], value[1], value[2]["timestamp"]))
    return valid[0][2]


def rewrite_archived_relative_candidates(
    candidates: list[dict[str, Any]], original_url: str, snapshot: str
) -> list[dict[str, Any]]:
    rewritten = []
    original_host = urlparse(original_url).netloc
    prefix = archive_url(snapshot, "")
    for candidate in candidates:
        candidate = dict(candidate)
        candidate_url = candidate.get("url", "")
        if urlparse(candidate_url).netloc == original_host:
            candidate["url"] = prefix + candidate_url
        rewritten.append(candidate)
    return rewritten


def merge_candidates(page: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    by_url = {candidate["url"]: candidate for candidate in page.get("candidates", [])}
    for candidate in candidates:
        current = by_url.get(candidate["url"])
        if current is None or candidate.get("score", 0) > current.get("score", 0):
            by_url[candidate["url"]] = candidate
    page["candidates"] = sorted(
        by_url.values(), key=lambda value: (-value.get("score", 0), value.get("url", ""))
    )


def page_cache_path(url: str) -> Path:
    return WAYBACK_ROOT / f"page-{collector.cache_key(url)}.json"


def load_archived_page_cache(url: str) -> dict[str, Any] | None:
    path = page_cache_path(url)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_archived_page_cache(url: str, payload: dict[str, Any]) -> None:
    WAYBACK_ROOT.mkdir(parents=True, exist_ok=True)
    page_cache_path(url).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def fetch_archived_candidates(
    client: httpx.AsyncClient,
    original_url: str,
    item: dict[str, Any],
    capture: dict[str, str],
    refresh: bool,
) -> dict[str, Any]:
    timestamp = capture["timestamp"]
    archived_url = archive_url(timestamp, original_url)
    if not refresh:
        cached = load_archived_page_cache(original_url)
        if (
            cached
            and cached.get("timestamp") == timestamp
            and cached.get("candidates")
        ):
            return cached
    try:
        response = await client.get(archived_url, follow_redirects=True, timeout=90)
        content_type = response.headers.get("content-type", "")
        if response.is_success and "html" in content_type:
            page_title, candidates = collector.extract_page_candidates(
                response.content, original_url, item
            )
            candidates = rewrite_archived_relative_candidates(candidates, original_url, timestamp)
            payload = {
                "original_url": original_url,
                "timestamp": timestamp,
                "archived_url": str(response.url),
                "status": response.status_code,
                "page_title": page_title,
                "candidates": candidates,
                "error": "",
            }
        else:
            payload = {
                "original_url": original_url,
                "timestamp": timestamp,
                "archived_url": str(response.url),
                "status": response.status_code,
                "page_title": "",
                "candidates": [],
                "error": f"HTTP {response.status_code} {content_type}",
            }
    except (httpx.HTTPError, UnicodeError) as exc:
        payload = {
            "original_url": original_url,
            "timestamp": timestamp,
            "archived_url": archived_url,
            "status": 0,
            "page_title": "",
            "candidates": [],
            "error": str(exc),
        }
    save_archived_page_cache(original_url, payload)
    return payload


def bulk_prefixes() -> list[str]:
    return [
        "www.lovelive-anime.jp/news/01_",
        *(f"www.lovelive-anime.jp/nijigasaki/news.php?id={digit}" for digit in "45678"),
        "www.lovelive-anime.jp/nijigasaki/live/live_detail.php?p=",
        "www.lovelive-anime.jp/nijigasaki/movie/detail.php?p=",
        *(f"www.lovelive-anime.jp/nijigasaki/detail.php?p=01_{digit}" for digit in "123456"),
        "www.lovelive-anime.jp/nijigasaki/sp_",
        "www.lovelive-anime.jp/lovelive-series2021/",
    ]


def load_manifest() -> dict[str, Any]:
    if not collector.MANIFEST_PATH.exists():
        return {"items": {}}
    try:
        return json.loads(collector.MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": {}}


async def recover(
    items: list[dict[str, Any]], concurrency: int, refresh: bool, candidate_limit: int, max_per_item: int
) -> None:
    manifest = load_manifest()
    item_map = manifest.get("items", {})
    target_items = [item for item in items if item_map.get(item["id"], {}).get("status") != "complete"]
    page_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in target_items:
        for link in item["official_links"]:
            url = canonical(link["url"])
            if is_lovelive_page(url):
                page_items[url].append(item)
    if not page_items:
        print("没有需要从 Wayback 恢复的官方页面。")
        return

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
    semaphore = asyncio.Semaphore(concurrency)
    captures_by_url: dict[str, list[dict[str, str]]] = defaultdict(list)
    async with httpx.AsyncClient(headers=headers, limits=limits) as client:
        async def load_prefix(prefix: str) -> None:
            async with semaphore:
                captures = await fetch_cdx(client, prefix, refresh=refresh)
            for capture in captures:
                original = canonical(capture.get("original", ""))
                captures_by_url[original].append(capture)
            print(f"CDX {prefix}: {len(captures)} 条")

        await asyncio.gather(*(load_prefix(prefix) for prefix in bulk_prefixes()))

        deep_urls = [
            url
            for url in page_items
            if any(
                marker in url
                for marker in (
                    "/nijigasaki/detail.php",
                    "/nijigasaki/live/",
                    "/nijigasaki/sp_",
                )
            )
        ]
        for index, url in enumerate(deep_urls, 1):
            async with semaphore:
                captures_by_url[url] = await fetch_cdx(
                    client, url, "exact", refresh, collapse=None
                )
            print(f"CDX deep {index}/{len(deep_urls)}: {len(captures_by_url[url])} {url}")

        missing = [url for url in page_items if url not in captures_by_url]
        for index, url in enumerate(missing, 1):
            async with semaphore:
                captures_by_url[url] = await fetch_cdx(
                    client, url, "exact", refresh, collapse=None
                )
            print(f"CDX exact {index}/{len(missing)}: {len(captures_by_url[url])} {url}")

        recovered_pages = 0
        for index, (url, associated_items) in enumerate(page_items.items(), 1):
            captures = captures_by_url.get(url, [])
            target = min(item_date(item) for item in associated_items)
            capture = choose_capture(captures, target)
            if not capture:
                continue
            async with semaphore:
                archived = await fetch_archived_candidates(client, url, associated_items[0], capture, refresh)
            if not archived.get("candidates"):
                continue
            page = collector.read_page_cache(url) or {
                "url": url,
                "status": 403,
                "final_url": url,
                "content_type": "text/html",
                "page_title": "",
                "candidates": [],
                "error": "",
            }
            merge_candidates(page, archived["candidates"])
            page["final_url"] = archived["archived_url"]
            page["wayback_timestamp"] = archived["timestamp"]
            page["wayback_url"] = archived["archived_url"]
            collector.write_page_cache(url, page)
            recovered_pages += 1
            print(f"恢复页面 {index}/{len(page_items)}: {len(archived['candidates'])} {url}")
    print(f"\n已恢复候选图片的页面：{recovered_pages}/{len(page_items)}")

    pages = await collector.fetch_all_pages(target_items, refresh=False, concurrency=concurrency)
    result = await collector.collect_images(
        target_items, pages, max_per_item, candidate_limit, concurrency
    )
    collector.summarize(result, target_items)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=12)
    parser.add_argument("--max-per-item", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    items = collector.load_items()
    asyncio.run(
        recover(
            items,
            concurrency=args.concurrency,
            refresh=args.refresh,
            candidate_limit=args.candidate_limit,
            max_per_item=args.max_per_item,
        )
    )


if __name__ == "__main__":
    main()
