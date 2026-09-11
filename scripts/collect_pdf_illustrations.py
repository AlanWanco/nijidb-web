#!/usr/bin/env python3
"""Extract illustration candidates embedded in official PDF links."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

import collect_collaboration_illustrations as collector

ROOT = Path(__file__).resolve().parents[1]
PDF_CACHE_ROOT = ROOT / "data/illustration-pdf-cache"
IMAGE_ROOT = collector.IMAGE_ROOT
MIN_PDF_IMAGE_BYTES = 8 * 1024
MAX_PDF_IMAGE_BYTES = 20 * 1024 * 1024


def pdf_urls(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for link in item["official_links"]:
            url = link["url"]
            if url.lower().split("?", 1)[0].endswith(".pdf"):
                result.setdefault(url, []).append(item)
    return result


def pdf_cache_path(url: str) -> Path:
    return PDF_CACHE_ROOT / f"{collector.cache_key(url)}.pdf"


def download_pdf(client: httpx.Client, url: str) -> Path | None:
    path = pdf_cache_path(url)
    if path.exists() and path.stat().st_size >= 1024:
        return path
    try:
        response = client.get(url, follow_redirects=True, timeout=90)
        content_type = response.headers.get("content-type", "")
        if not response.is_success or not response.content.startswith(b"%PDF"):
            print(f"PDF 失败 {response.status_code} {content_type}: {url}")
            return None
        PDF_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        print(f"PDF 下载 {len(response.content)} bytes: {url}")
        return path
    except httpx.HTTPError as exc:
        print(f"PDF 异常 {exc}: {url}")
        return None


def pdf_image_records(pdf_path: Path) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="nijidb-pdf-images-") as temporary:
        prefix = Path(temporary) / "image"
        try:
            subprocess.run(
                ["pdfimages", "-all", str(pdf_path), str(prefix)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"pdfimages 失败 {pdf_path}: {exc}")
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(Path(temporary).glob("image-*")):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) < MIN_PDF_IMAGE_BYTES or len(data) > MAX_PDF_IMAGE_BYTES:
                continue
            width, height = collector.image_dimensions(data)
            if min(width, height) < collector.MIN_IMAGE_SIDE:
                continue
            if not width or not height:
                continue
            try:
                number = int(path.stem.rsplit("-", 1)[1])
            except ValueError:
                continue
            records.append(
                {
                    "number": number,
                    "path": path,
                    "data": data,
                    "width": width,
                    "height": height,
                    "bytes": len(data),
                }
            )
        return records


def selected_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    def score(record: dict[str, Any]) -> tuple[float, int]:
        width = record["width"]
        height = record["height"]
        area = width * height
        aspect_penalty = 0.35 if max(width, height) / min(width, height) > 3.2 else 1
        return (area * aspect_penalty, record["number"])

    return sorted(records, key=score, reverse=True)[:limit]


def asset_extension(record: dict[str, Any]) -> str:
    suffix = record["path"].suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else ".jpg"


def add_pdf_images(
    manifest: dict[str, Any],
    items_by_pdf: dict[str, list[dict[str, Any]]],
    records_by_pdf: dict[str, list[dict[str, Any]]],
    max_per_item: int,
) -> int:
    manifest.setdefault("items", {})
    manifest.setdefault("assets", {})
    added = 0
    for pdf_url, items in items_by_pdf.items():
        records = records_by_pdf.get(pdf_url, [])
        if not records:
            continue
        for item in items:
            entry = manifest["items"].setdefault(item["id"], {})
            images = [
                image
                for image in entry.get("images", [])
                if not collector.is_site_chrome_url(image.get("source_url", ""))
            ]
            known_hashes = {image.get("sha256") for image in images}
            for record in selected_records(records, max_per_item * 2):
                if len(images) >= max_per_item:
                    break
                digest = hashlib.sha256(record["data"]).hexdigest()
                if digest in known_hashes:
                    continue
                extension = asset_extension(record)
                relative_path = f"assets/{digest}{extension}"
                local_path = IMAGE_ROOT / relative_path
                if not local_path.exists():
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(record["data"])
                manifest["assets"].setdefault(
                    digest,
                    {
                        "path": f"/media/illustrations/{relative_path}",
                        "width": record["width"],
                        "height": record["height"],
                        "bytes": record["bytes"],
                    },
                )
                source_url = f"{pdf_url}#page-image-{record['number']}"
                images.append(
                    {
                        "path": f"/media/illustrations/{relative_path}",
                        "source_url": source_url,
                        "source_page": pdf_url,
                        "sha256": digest,
                        "width": record["width"],
                        "height": record["height"],
                        "bytes": record["bytes"],
                        "score": 70,
                        "kind": "pdf_image",
                        "context": "PDF 内嵌图片",
                    }
                )
                known_hashes.add(digest)
                added += 1
            images = images[:max_per_item]
            entry["images"] = images
            entry["status"] = (
                "complete" if len(images) >= max_per_item else "partial" if images else "unavailable"
            )
            entry["candidate_count"] = max(entry.get("candidate_count", 0), len(records))
            print(f"PDF 图片 {len(images)}/{max_per_item}: {item['title'].splitlines()[0]}")
    return added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-item", type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    items = collector.load_items()
    items_by_pdf = pdf_urls(items)
    manifest = collector.load_existing_manifest()
    with httpx.Client(headers={"User-Agent": collector.USER_AGENT, "Accept-Language": "ja,en;q=0.8"}) as client:
        records_by_pdf = {}
        for url in items_by_pdf:
            pdf_path = download_pdf(client, url)
            records_by_pdf[url] = pdf_image_records(pdf_path) if pdf_path else []
            print(f"PDF 候选 {len(records_by_pdf[url])}: {url}")
    added = add_pdf_images(manifest, items_by_pdf, records_by_pdf, args.max_per_item)
    manifest["generated_at"] = collector.now_iso()
    collector.write_manifest(manifest)
    print(f"\nPDF 采集完成，新增图片：{added}，清单：{collector.MANIFEST_PATH}")


if __name__ == "__main__":
    main()
