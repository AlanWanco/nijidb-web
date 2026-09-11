#!/usr/bin/env python3
"""Replace collaboration gallery images from the local official-page archive.

This script intentionally performs no network access. It matches each catalog
record to its local official Markdown page, imports every image referenced by
that page, and de-duplicates identical files by SHA-256. Records without a local
page are left untouched; existing local assets are never deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .collect_collaboration_illustrations import (
        IMAGE_ROOT,
        MANIFEST_PATH,
        MAX_IMAGE_BYTES,
        detected_content_type,
        image_dimensions,
        image_extension,
        load_existing_manifest,
        now_iso,
        write_manifest,
    )
except ImportError:  # pragma: no cover - supports running this file directly
    from collect_collaboration_illustrations import (
        IMAGE_ROOT,
        MANIFEST_PATH,
        MAX_IMAGE_BYTES,
        detected_content_type,
        image_dimensions,
        image_extension,
        load_existing_manifest,
        now_iso,
        write_manifest,
    )

try:
    from .collaboration_urls import dedupe_page_urls, page_identity
except ImportError:  # pragma: no cover - supports running this file directly
    from collaboration_urls import dedupe_page_urls, page_identity

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "frontend/src/content/collaborationIllustrations.json"
DEFAULT_SOURCE_DIR = Path("/Volumes/SSK/Download/bangumi-parser/ll-offical-site")


def load_items() -> list[dict[str, Any]]:
    payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return [item for year in payload["years"] for item in year["items"]]


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "euc_jis_2004", "shift_jis"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def page_id_from_url(url: str) -> str | None:
    match = re.search(r"[?&]id=(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"(?:[?&]p=|/)(01_\d+)(?:\.html)?(?:[/#?]|$)", url)
    return match.group(1) if match else None


def page_id_from_document(text: str) -> str | None:
    match = re.search(r"^- 页面名称：([^\n]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def page_source_from_document(text: str) -> str:
    match = re.search(r"^- 来源：(https?://\S+)", text, re.MULTILINE)
    return match.group(1).rstrip(")]") if match else ""


def document_title(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else "本地官方页面"


def local_image_names(text: str) -> list[str]:
    names: list[str] = []
    for raw_name in re.findall(r"pic/([^\s)]+)", text):
        name = raw_name.rstrip("]")
        if name not in names:
            names.append(name)
    return names


def image_source_url(text: str, name: str, fallback: str) -> str:
    pattern = rf"pic/{re.escape(name)}\)\]\((https?://[^)]+)\)"
    match = re.search(pattern, text)
    return match.group(1) if match else fallback


def source_url_key(url: str) -> str:
    return page_identity(url).rstrip("/")


def build_documents(
    source_dir: Path,
) -> tuple[dict[str, tuple[Path, str]], dict[str, tuple[Path, str]]]:
    documents_by_id: dict[str, tuple[Path, str]] = {}
    documents_by_source: dict[str, tuple[Path, str]] = {}
    for path in sorted(source_dir.glob("*.md")):
        text = read_text(path)
        document = (path, text)
        page_id = page_id_from_document(text)
        if page_id and page_id not in documents_by_id:
            documents_by_id[page_id] = document
        source_url = page_source_from_document(text)
        if source_url and source_url_key(source_url) not in documents_by_source:
            documents_by_source[source_url_key(source_url)] = document
    return documents_by_id, documents_by_source


def matching_documents(
    item: dict[str, Any],
    documents_by_id: dict[str, tuple[Path, str]],
    documents_by_source: dict[str, tuple[Path, str]],
) -> list[tuple[Path, str]]:
    matches: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    for link in item["official_links"]:
        page_id = page_id_from_url(link["url"])
        candidates = []
        if page_id and page_id in documents_by_id:
            candidates.append(documents_by_id[page_id])
        source_document = documents_by_source.get(source_url_key(link["url"]))
        if source_document:
            candidates.append(source_document)
        for document in candidates:
            if document[0] not in seen_paths:
                seen_paths.add(document[0])
                matches.append(document)
    return matches


def local_asset_exists(image: dict[str, Any]) -> bool:
    path = image.get("path", "")
    prefix = "/media/illustrations/"
    return path.startswith(prefix) and (IMAGE_ROOT / path.removeprefix(prefix)).is_file()


def add_local_image(
    manifest: dict[str, Any],
    item: dict[str, Any],
    document_path: Path,
    document_text: str,
    source_path: Path,
    source_url: str,
    dry_run: bool,
) -> bool:
    data = source_path.read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        print(f"跳过 {source_path.name}: 文件大小 {len(data)}")
        return False
    width, height = image_dimensions(data)
    if not width or not height:
        print(f"跳过 {source_path.name}: 无法解析尺寸 {width}x{height}")
        return False

    digest = hashlib.sha256(data).hexdigest()
    record = manifest["items"].setdefault(
        item["id"],
        {
            "first_seen": item["first_seen"],
            "title": item["title"],
            "images": [],
            "status": "unavailable",
            "attempted_pages": dedupe_page_urls([link["url"] for link in item["official_links"]]),
            "candidate_count": 0,
            "failures": [],
        },
    )
    record["first_seen"] = item["first_seen"]
    record["title"] = item["title"]
    record["attempted_pages"] = dedupe_page_urls([link["url"] for link in item["official_links"]])
    images = record.setdefault("images", [])
    if any(image.get("sha256") == digest for image in images):
        return False

    content_type = detected_content_type(data, mimetypes.guess_type(source_path.name)[0] or "", source_url)
    extension = image_extension(data, content_type, source_url)
    asset_path = f"assets/{digest}{extension}"
    absolute_asset_path = IMAGE_ROOT / asset_path
    if not dry_run and not absolute_asset_path.exists():
        absolute_asset_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_asset_path.write_bytes(data)

    manifest["assets"].setdefault(
        digest,
        {
            "path": f"/media/illustrations/{asset_path}",
            "width": width,
            "height": height,
            "bytes": len(data),
        },
    )
    images.append(
        {
            "path": f"/media/illustrations/{asset_path}",
            "source_url": source_url,
            "source_page": page_source_from_document(document_text) or source_url,
            "sha256": digest,
            "width": width,
            "height": height,
            "bytes": len(data),
            "score": 95,
            "kind": "local_official",
            "context": f"本地官方页面：{document_title(document_text)}；文件：{document_path.name}",
        }
    )
    return True


def collect(
    source_dir: Path,
    item_id: str | None,
    dry_run: bool,
) -> tuple[dict[str, Any], Counter[str], int]:
    documents_by_id, documents_by_source = build_documents(source_dir)
    items = load_items()
    if item_id:
        items = [item for item in items if item["id"] == item_id]
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
    statuses: Counter[str] = Counter()
    imported = 0
    matched_count = 0
    replaced_count = 0
    unmatched_count = 0

    for item in items:
        documents = matching_documents(item, documents_by_id, documents_by_source)
        previous = manifest["items"].get(item["id"], {})
        if not documents:
            unmatched_count += 1
            if previous:
                statuses[previous.get("status", "unavailable")] += 1
            continue
        matched_count += 1

        page_urls = dedupe_page_urls([link["url"] for link in item["official_links"]])
        references: list[tuple[Path, str, str, str]] = []
        seen_references: set[tuple[Path, str]] = set()
        for document_path, document_text in documents:
            fallback_url = page_source_from_document(document_text) or page_urls[0]
            for name in local_image_names(document_text):
                reference_key = (document_path, name)
                if reference_key in seen_references:
                    continue
                seen_references.add(reference_key)
                source_path = source_dir / "pic" / name
                source_url = image_source_url(document_text, name, fallback_url)
                references.append((document_path, document_text, source_path, source_url))

        record = manifest["items"].setdefault(
            item["id"],
            {
                "first_seen": item["first_seen"],
                "title": item["title"],
                "images": [],
                "status": "unavailable",
                "attempted_pages": page_urls,
                "candidate_count": 0,
                "failures": [],
            },
        )
        record["first_seen"] = item["first_seen"]
        record["title"] = item["title"]
        record["attempted_pages"] = page_urls
        record["candidate_count"] = len(references)
        record["failures"] = []

        available_references = [reference for reference in references if reference[2].is_file()]
        missing_references = [reference for reference in references if not reference[2].is_file()]
        record["failures"] = [
            {"url": source_url, "error": "本地图片文件不存在"}
            for _, _, _, source_url in missing_references
        ][:20]

        # A local page with archived images is authoritative over old scraped
        # candidates. Explicitly user-supplied verified images are retained as
        # manual additions, so a later archive refresh cannot silently discard
        # them.
        previous_images = previous.get("images", [])
        manual_images = [
            image
            for image in previous_images
            if image.get("kind") == "manual_official" and (dry_run or local_asset_exists(image))
        ]
        if available_references:
            record["images"] = manual_images
            replaced_count += 1
        else:
            record["images"] = manual_images

        for document_path, document_text, source_path, source_url in available_references:
            if add_local_image(
                manifest,
                item,
                document_path,
                document_text,
                source_path,
                source_url,
                dry_run,
            ):
                imported += 1

        images = record.get("images", [])
        if not dry_run:
            images = [image for image in images if local_asset_exists(image)]
        if images:
            record["status"] = "partial" if record["failures"] or not available_references else "complete"
        else:
            record["status"] = "unavailable"
        statuses[record["status"]] += 1

    print(
        f"本地页面匹配：{matched_count} 条；替换图片记录：{replaced_count} 条；"
        f"未匹配：{unmatched_count} 条"
    )
    if not dry_run:
        write_manifest(manifest)
    return manifest, statuses, imported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="ll-offical-site 本地目录",
    )
    parser.add_argument("--item-id", help="只处理一个 manifest 项目 ID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest, statuses, imported = collect(args.source_dir, args.item_id, args.dry_run)
    print(f"本地导入：{imported} 张")
    print(f"本次处理状态：{dict(statuses)}")
    print(f"清单项目：{len(manifest.get('items', {}))}；资源：{len(manifest.get('assets', {}))}")
    if args.dry_run:
        print("dry-run：未写入图片和 manifest")
    else:
        print(f"清单：{MANIFEST_PATH}")


if __name__ == "__main__":
    main()
