#!/usr/bin/env python3
"""为生产迁移清除数据库中的本地图片路径。

脚本只修改 SQLite 引用，不删除图片文件。新闻优先保留已有的 R2 public_url
或远程 source_url；只有没有远程来源的本地新闻图片才会从图库关联中移除。联动图片必须已经有
R2 public_url 或可访问的 source_url，否则脚本会在写入前中止，避免生产出现
静默丢图。

默认只检查并报告；真正写入必须显式传入 --apply。写入前会使用 SQLite 在线
备份生成一个同目录、权限为 600 的备份文件。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


def valid_external_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_args() -> argparse.Namespace:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=data_dir / "nijidb.sqlite3")
    parser.add_argument("--apply", action="store_true", help="写入修改；不传时只检查并报告")
    return parser.parse_args()


def backup_database(database: Path) -> Path:
    handle, raw_path = tempfile.mkstemp(
        prefix=f"{database.stem}-before-local-path-strip-",
        suffix=database.suffix or ".sqlite3",
        dir=database.parent,
    )
    os.close(handle)
    backup = Path(raw_path)
    source = sqlite3.connect(database)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
        destination.commit()
        os.chmod(backup, 0o600)
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    finally:
        destination.close()
        source.close()
    return backup


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    )


def column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def inspect_database(database: Path) -> dict[str, int | list[str]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        unresolved: list[str] = []
        news_clear = news_delete = collabo_clear = 0
        if table_exists(connection, "news_images"):
            public_column = "public_url" if column_exists(connection, "news_images", "public_url") else "''"
            for row in connection.execute(
                f"SELECT id, local_path, source_url, {public_column} AS public_url "
                "FROM news_images WHERE local_path != ''"
            ):
                if valid_external_url(row["public_url"]) or valid_external_url(row["source_url"]):
                    news_clear += 1
                else:
                    news_delete += 1
        if table_exists(connection, "collaboration_images"):
            for row in connection.execute(
                "SELECT id, path, thumbnail_path, public_url, source_url FROM collaboration_images "
                "WHERE path != '' OR thumbnail_path != ''"
            ):
                if valid_external_url(row["public_url"]) or valid_external_url(row["source_url"]):
                    collabo_clear += 1
                else:
                    unresolved.append(f"联动图片 {row['id']} 没有 R2 public_url 或远程 source_url")
        release_local = 0
        release_html_local = 0
        if table_exists(connection, "releases"):
            release_local = connection.execute(
                "SELECT COUNT(*) FROM releases WHERE cover_url LIKE '/media/%'"
            ).fetchone()[0]
            release_html_local = sum(
                1
                for row in connection.execute("SELECT id, detail_html FROM releases")
                if re.search(r"/media/", str(row["detail_html"] or ""))
            )
            if release_local or release_html_local:
                unresolved.append(
                    f"仍有 {release_local} 个本地封面和 {release_html_local} 条发行详情包含 /media/，请先完成 R2 改写"
                )
        return {
            "news_clear": news_clear,
            "news_delete": news_delete,
            "collabo_clear": collabo_clear,
            "release_local": release_local,
            "release_html_local": release_html_local,
            "unresolved": unresolved,
        }
    finally:
        connection.close()


def apply_changes(database: Path) -> tuple[Path, dict[str, int]]:
    report = inspect_database(database)
    unresolved = report["unresolved"]
    if unresolved:
        raise RuntimeError("\n".join(str(item) for item in unresolved))
    backup = backup_database(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        news_clear = news_delete = collabo_clear = 0
        if table_exists(connection, "news_images"):
            public_column = "public_url" if column_exists(connection, "news_images", "public_url") else "''"
            rows = connection.execute(
                f"SELECT id, source_url, {public_column} AS public_url FROM news_images WHERE local_path != ''"
            ).fetchall()
            for row in rows:
                if valid_external_url(row["public_url"]) or valid_external_url(row["source_url"]):
                    connection.execute("UPDATE news_images SET local_path = '' WHERE id = ?", (row["id"],))
                    news_clear += 1
                else:
                    connection.execute("DELETE FROM news_images WHERE id = ?", (row["id"],))
                    news_delete += 1
        if table_exists(connection, "collaboration_images"):
            rows = connection.execute(
                "SELECT id FROM collaboration_images WHERE path != '' OR thumbnail_path != ''"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE collaboration_images SET path = '', thumbnail_path = '' WHERE id = ?",
                    (row["id"],),
                )
                collabo_clear += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return backup, {"news_clear": news_clear, "news_delete": news_delete, "collabo_clear": collabo_clear}


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在：{database}")
    report = inspect_database(database)
    print(
        f"检查结果：新闻保留 R2/远程引用 {report['news_clear']} 条，"
        f"新闻将移除无远程来源 {report['news_delete']} 条，"
        f"联动将清除本地路径 {report['collabo_clear']} 条"
    )
    for item in report["unresolved"]:
        print(f"[error] {item}")
    if not args.apply:
        print("仅检查，未修改数据库；确认 R2 和远程来源无误后再传入 --apply。")
        return 1 if report["unresolved"] else 0
    if report["unresolved"]:
        return 2
    backup, changed = apply_changes(database)
    print(
        f"已清除本地图片路径：新闻保留 R2/远程 {changed['news_clear']} 条、"
        f"移除无远程来源 {changed['news_delete']} 条、联动 {changed['collabo_clear']} 条；"
        f"数据库备份：{backup}；时间：{datetime.now(UTC).isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
