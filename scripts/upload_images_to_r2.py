#!/usr/bin/env python3
"""Upload the local image cache to Cloudflare R2.

Credentials are read from environment variables. With --rewrite-db, the
SQLite database is backed up and local /media references are replaced with
the configured public R2 base URL.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import boto3


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"缺少环境变量：{name}")
    return value


def parse_args() -> argparse.Namespace:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    parser = argparse.ArgumentParser(description="将 Nijidb 图片缓存全量上传到 Cloudflare R2")
    parser.add_argument("--image-dir", type=Path, default=data_dir / "images")
    parser.add_argument("--db-path", type=Path, default=data_dir / "nijidb.sqlite3")
    parser.add_argument("--endpoint-url", default=os.getenv("R2_ENDPOINT", ""))
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET", "nijidb"))
    parser.add_argument("--prefix", default=os.getenv("R2_IMAGE_PREFIX", "images"))
    parser.add_argument("--public-base-url", default=os.getenv("R2_PUBLIC_BASE_URL", ""))
    parser.add_argument("--rewrite-db", action="store_true", help="上传成功后将数据库中的图片引用改为公开 R2 URL")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要上传的文件，不执行上传或数据库修改")
    return parser.parse_args()


def image_files(image_dir: Path) -> list[Path]:
    if not image_dir.is_dir():
        raise SystemExit(f"图片目录不存在：{image_dir}")
    return sorted(
        path for path in image_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in SUPPORTED_EXTENSIONS
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
    for index, path in enumerate(files, 1):
        key = object_key(path, args.image_dir, args.prefix)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.upload_file(
            str(path),
            args.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": "no-cache"},
        )
        print(f"[{index}/{len(files)}] {key}")

    if args.rewrite_db:
        backup_path, updated = rewrite_database(args.db_path, args.image_dir, args.prefix, base_url)
        print(f"已更新 {updated} 条图片引用；数据库备份：{backup_path}")
    print(f"上传完成：{datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
