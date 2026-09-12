#!/usr/bin/env python3
"""Upload and maintain the local image cache in Cloudflare R2.

Credentials are read from environment variables. With --rewrite-db, the
SQLite database is backed up and local image references receive the configured
public R2 URL while local files remain as a fallback copy. The explicit
--delete-unused-news option removes only unreferenced news objects.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import boto3

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".avif"}
MACOS_METADATA_NAMES = {".DS_Store", ".localized"}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"缺少环境变量：{name}")
    return value


def parse_args() -> argparse.Namespace:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    parser = argparse.ArgumentParser(description="将 Nijidb 图片缓存增量上传到 Cloudflare R2")
    parser.add_argument("--image-dir", type=Path, default=data_dir / "images")
    parser.add_argument("--db-path", type=Path, default=data_dir / "nijidb.sqlite3")
    parser.add_argument("--endpoint-url", default=os.getenv("R2_ENDPOINT", ""))
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET", "nijidb"))
    parser.add_argument("--prefix", default=os.getenv("R2_IMAGE_PREFIX", "images"))
    parser.add_argument("--public-base-url", default=os.getenv("R2_PUBLIC_BASE_URL", ""))
    parser.add_argument("--rewrite-db", action="store_true", help="上传成功后为数据库图片写入公开 R2 URL")
    parser.add_argument(
        "--delete-unused-news",
        "--cleanup-unused-news",
        dest="delete_unused_news",
        action="store_true",
        help="删除 R2 中未被数据库引用的新闻对象（仅清理 news/news-archive）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出将要上传的文件，不执行上传或数据库修改")
    return parser.parse_args()


def image_files(image_dir: Path) -> list[Path]:
    if not image_dir.is_dir():
        raise SystemExit(f"图片目录不存在：{image_dir}")
    return sorted(
        path
        for path in image_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith("._")
        and path.name not in MACOS_METADATA_NAMES
    )


def object_key(path: Path, image_dir: Path, prefix: str) -> str:
    relative = path.relative_to(image_dir).as_posix()
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{relative}" if clean_prefix else relative


def public_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(key, safe='/')}"


def backup_database(db_path: Path) -> Path:
    handle, raw_path = tempfile.mkstemp(
        prefix=f"{db_path.stem}-before-r2-",
        suffix=db_path.suffix or ".sqlite3",
        dir=db_path.parent,
    )
    os.close(handle)
    backup_path = Path(raw_path)
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        destination.close()
        source.close()
    return backup_path


def collaboration_image_path(image_dir: Path, stored_path: str) -> Path | None:
    relative = stored_path.removeprefix("/media/illustrations/").removeprefix("illustrations/").lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return None
    root = image_dir if image_dir.name == "illustrations" else image_dir / "illustrations"
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def news_image_relative_path(stored_path: str) -> str:
    raw = str(stored_path or "").strip()
    if not raw:
        return ""
    if raw.startswith("runtime:"):
        root = "news"
        raw = raw.removeprefix("runtime:").lstrip("/")
    else:
        root = "news-archive"
        raw = raw.removeprefix("archive:").lstrip("/")
    if not raw or ".." in Path(raw).parts:
        return ""
    return f"{root}/{Path(raw).as_posix()}"


def news_image_path(image_dir: Path, stored_path: str) -> Path | None:
    relative = news_image_relative_path(stored_path)
    if not relative:
        return None
    root = image_dir.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() else None


def r2_key_from_url(value: str, prefix: str) -> str:
    parsed = urlparse(str(value or "").strip())
    path = unquote(parsed.path).lstrip("/")
    clean_prefix = prefix.strip("/")
    if not clean_prefix:
        return path
    return path if path.startswith(f"{clean_prefix}/") else ""


def news_database_object_keys(connection: sqlite3.Connection, prefix: str) -> set[str]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'news_images'"
    ).fetchone():
        return set()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news_images)")}
    public_column = "public_url" if "public_url" in columns else "''"
    keys: set[str] = set()
    for row in connection.execute(f"SELECT local_path, {public_column} AS public_url FROM news_images"):
        local_relative = news_image_relative_path(str(row[0] or ""))
        if local_relative:
            keys.add(object_key_for_relative(local_relative, prefix))
        public_key = r2_key_from_url(str(row[1] or ""), prefix)
        if public_key:
            keys.add(public_key)
    return keys


def object_key_for_relative(relative: str, prefix: str) -> str:
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{relative}" if clean_prefix else relative


def delete_unused_news_objects(
    client, bucket: str, prefix: str, db_path: Path, existing: dict[str, int]
) -> tuple[int, int]:
    connection = sqlite3.connect(db_path)
    try:
        used = news_database_object_keys(connection, prefix)
    finally:
        connection.close()
    clean_prefix = prefix.strip("/")
    roots = tuple(object_key_for_relative(root, clean_prefix).rstrip("/") + "/" for root in ("news", "news-archive"))
    unused = sorted(
        key for key in existing if key.startswith(roots) and key not in used
    )
    bytes_removed = sum(existing[key] for key in unused)
    for start in range(0, len(unused), 1000):
        batch = unused[start : start + 1000]
        if not batch:
            continue
        response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            details = ", ".join(str(item.get("Key") or "") for item in errors[:5])
            raise RuntimeError(f"删除 R2 新闻图片失败：{details}")
    print(
        f"R2 新闻图片清理：数据库引用 {len(used)} 个，删除 {len(unused)} 个，共 {bytes_removed:,} bytes"
    )
    return len(unused), bytes_removed


def rewrite_news_images(connection: sqlite3.Connection, image_dir: Path, prefix: str, base_url: str) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'news_images'"
    ).fetchone()
    if not table:
        return 0
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news_images)")}
    if "public_url" not in columns:
        connection.execute("ALTER TABLE news_images ADD COLUMN public_url TEXT NOT NULL DEFAULT ''")
    updated = 0
    rows = connection.execute(
        "SELECT id, local_path FROM news_images WHERE local_path != '' ORDER BY news_id, position, id"
    ).fetchall()
    for row in rows:
        image_path = news_image_path(image_dir, str(row["local_path"] or ""))
        if not image_path:
            print(f"[warning] 找不到新闻图片，跳过 URL 改写：{row['local_path']}")
            continue
        key = object_key(image_path, image_dir, prefix)
        connection.execute(
            "UPDATE news_images SET public_url = ? WHERE id = ?",
            (public_url(base_url, key), row["id"]),
        )
        updated += 1
    return updated


def rewrite_collaboration_images(connection: sqlite3.Connection, image_dir: Path, prefix: str, base_url: str) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collaboration_images'"
    ).fetchone()
    if not table:
        return 0
    columns = {row[1] for row in connection.execute("PRAGMA table_info(collaboration_images)")}
    if "public_url" not in columns:
        connection.execute("ALTER TABLE collaboration_images ADD COLUMN public_url TEXT NOT NULL DEFAULT ''")
    updated = 0
    rows = connection.execute(
        "SELECT id, path FROM collaboration_images WHERE path != '' ORDER BY item_id, position, id"
    ).fetchall()
    for row in rows:
        image_path = collaboration_image_path(image_dir, str(row["path"] or ""))
        if not image_path:
            print(f"[warning] 找不到联动图片，跳过 URL 改写：{row['path']}")
            continue
        key = object_key(image_path, image_dir, prefix)
        connection.execute(
            "UPDATE collaboration_images SET public_url = ? WHERE id = ?",
            (public_url(base_url, key), row["id"]),
        )
        updated += 1
    return updated


def existing_object_sizes(client, bucket: str, prefix: str) -> dict[str, int]:
    clean_prefix = prefix.strip("/")
    listing_prefix = f"{clean_prefix}/" if clean_prefix else ""
    objects: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=listing_prefix):
        for item in page.get("Contents", []):
            objects[str(item["Key"])] = int(item.get("Size") or 0)
    return objects


def rewrite_database(db_path: Path, image_dir: Path, prefix: str, base_url: str) -> tuple[Path, int]:
    backup_path = backup_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    updated = 0
    try:
        rows = connection.execute("SELECT id, cover_url, detail_html FROM releases ORDER BY position, id").fetchall()
        for row in rows:
            current_url = row["cover_url"] or ""
            filename = Path(urlparse(current_url).path).name
            image_path = image_dir / filename if filename else None
            if not image_path or not image_path.is_file():
                candidates = [image_dir / f"{row['id']}{extension}" for extension in SUPPORTED_EXTENSIONS]
                image_path = next((path for path in candidates if path.is_file()), None)
            if not image_path:
                raise RuntimeError(f"找不到发行 {row['id']} 对应的本地封面")
            key = object_key(image_path, image_dir, prefix)
            next_url = public_url(base_url, key)
            detail_html = (row["detail_html"] or "").replace(current_url, next_url)
            detail_html = detail_html.replace(f"/media/{image_path.name}", next_url)
            connection.execute(
                "UPDATE releases SET cover_url = ?, detail_html = ? WHERE id = ?",
                (next_url, detail_html, row["id"]),
            )
            updated += 1
        updated += rewrite_news_images(connection, image_dir, prefix, base_url)
        updated += rewrite_collaboration_images(connection, image_dir, prefix, base_url)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return backup_path, updated


def main() -> None:
    args = parse_args()
    endpoint = args.endpoint_url.strip() or required_env("R2_ENDPOINT")
    access_key = required_env("R2_ACCESS_KEY_ID")
    secret_key = required_env("R2_SECRET_ACCESS_KEY")
    base_url = args.public_base_url.strip()
    if args.rewrite_db:
        if not base_url:
            raise SystemExit("--rewrite-db 需要 --public-base-url 或 R2_PUBLIC_BASE_URL")
        if base_url.rstrip("/") == endpoint.rstrip("/"):
            raise SystemExit("R2_PUBLIC_BASE_URL 不能直接使用 R2 S3 API Endpoint，请填写 r2.dev 或自定义域名")
    files = image_files(args.image_dir)
    if args.delete_unused_news:
        connection = sqlite3.connect(args.db_path)
        try:
            used_news_keys = news_database_object_keys(connection, args.prefix)
        finally:
            connection.close()
        clean_prefix = args.prefix.strip("/")
        news_roots = tuple(
            object_key_for_relative(root, clean_prefix).rstrip("/") + "/"
            for root in ("news", "news-archive")
        )
        before_count = len(files)
        files = [
            path
            for path in files
            if not object_key(path, args.image_dir, args.prefix).startswith(news_roots)
            or object_key(path, args.image_dir, args.prefix) in used_news_keys
        ]
        if len(files) != before_count:
            print(f"跳过 {before_count - len(files)} 个未被数据库引用的本地新闻图片")
    total_bytes = sum(path.stat().st_size for path in files)
    print(f"准备上传 {len(files)} 个文件，共 {total_bytes:,} bytes")
    if args.dry_run:
        for path in files:
            print(f"[dry-run] {path.relative_to(args.image_dir)}")
        return

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.getenv("R2_REGION", "auto"),
    )
    existing = existing_object_sizes(client, args.bucket, args.prefix)
    skipped = 0
    uploaded = 0
    print(f"发现 {len(existing)} 个已有 R2 对象，将按 key 和文件大小跳过可复用对象")
    for index, path in enumerate(files, 1):
        key = object_key(path, args.image_dir, args.prefix)
        if existing.get(key) == path.stat().st_size:
            skipped += 1
            print(f"[skip {index}/{len(files)}] {key}")
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.upload_file(
            str(path),
            args.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": "no-cache"},
        )
        uploaded += 1
        print(f"[{index}/{len(files)}] {key}")

    if args.rewrite_db:
        backup_path, updated = rewrite_database(args.db_path, args.image_dir, args.prefix, base_url)
        print(f"已更新 {updated} 条图片引用；数据库备份：{backup_path}")
    if args.delete_unused_news:
        delete_unused_news_objects(client, args.bucket, args.prefix, args.db_path, existing)
    print(f"上传完成：{datetime.now(UTC).isoformat()}；新上传 {uploaded} 个，跳过 {skipped} 个")


if __name__ == "__main__":
    main()
