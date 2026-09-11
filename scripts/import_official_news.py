#!/usr/bin/env python3
"""将官网本地 Markdown 新闻导入 Nijidb 的独立新闻表。

默认只把图片记录为归档目录中的相对路径，不复制 6GB 左右的本地图库。
运行应用时请设置 NEWS_ARCHIVE_DIR 指向同一个 ll-offical-site 目录；如果要
把图片带进数据卷，可额外使用 --copy-images。

示例：
    uv run --locked python scripts/import_official_news.py \
      --root /Volumes/SSK/Download/bangumi-parser/ll-offical-site
    uv run --locked python scripts/import_official_news.py --limit 3
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.news import ensure_news_schema, parse_local_markdown, upsert_news_record  # noqa: E402

DEFAULT_ROOT = Path("/Volumes/SSK/Download/bangumi-parser/ll-offical-site")
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "nijidb.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.getenv("NEWS_ARCHIVE_DIR", str(DEFAULT_ROOT))),
        help="本地 Markdown 目录，默认读取 NEWS_ARCHIVE_DIR 或外接盘归档目录",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("NEWS_DATABASE", str(DEFAULT_DATABASE))),
        help="SQLite 文件，默认是项目 data/nijidb.sqlite3",
    )
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 个文件，用于试运行")
    parser.add_argument("--source", choices=("niji_topics", "niji_news", "as_news"), help="只导入一种来源")
    parser.add_argument("--copy-images", action="store_true", help="将图片复制到数据库旁的 images/news 目录")
    return parser.parse_args()


def copy_images(record: dict, archive_root: Path, target_root: Path) -> tuple[int, int]:
    copied = 0
    missing = 0
    for image in record.get("images") or []:
        if image.get("kind") != "archive":
            continue
        relative = Path(str(image.get("local_path") or ""))
        source = (archive_root / relative).resolve()
        try:
            source.relative_to(archive_root.resolve())
        except ValueError:
            missing += 1
            continue
        if not source.is_file():
            missing += 1
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        extension = source.suffix.lower() or ".bin"
        target_relative = Path("assets") / f"{digest}{extension}"
        target = target_root / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
            copied += 1
        image["local_path"] = f"runtime:{target_relative.as_posix()}"
    return copied, missing


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"未找到新闻归档目录：{root}")
    files = sorted(path for path in root.glob("*.md") if not path.name.startswith("._"))
    if args.source:
        files = [path for path in files if args.source in path.name]
    if args.limit:
        files = files[: max(0, args.limit)]
    if not files:
        raise SystemExit(f"没有可导入的 Markdown：{root}")

    database = args.database.expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    runtime_images = database.parent / "images" / "news"
    created = updated = copied = missing = 0
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_news_schema(connection)
        for path in files:
            record = parse_local_markdown(path)
            if args.copy_images:
                image_copied, image_missing = copy_images(record, root, runtime_images)
                copied += image_copied
                missing += image_missing
            with connection:
                was_changed, was_created = upsert_news_record(connection, record)
            created += int(was_created)
            updated += int(was_changed and not was_created)
            if (created + updated) % 250 == 0:
                print(f"已处理 {created + updated}/{len(files)}", flush=True)
    finally:
        connection.close()

    print(
        f"导入完成：{len(files)} 条，新增 {created} 条，更新 {updated} 条；"
        f"复制图片 {copied} 张，缺失图片 {missing} 张；数据库：{database}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
