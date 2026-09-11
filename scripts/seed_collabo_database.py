#!/usr/bin/env python3
"""Seed the collaboration SQLite tables from the local index and manifest."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.collabo import ensure_collaboration_schema, migrate_collaboration_sources  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/data/nijidb.sqlite3"))
    parser.add_argument("--index", type=Path, required=True, help="collaborationIllustrations.json")
    parser.add_argument("--manifest", type=Path, required=True, help="illustrations/manifest.json")
    args = parser.parse_args()

    args.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_collaboration_schema(conn)
        result = migrate_collaboration_sources(conn, args.index, args.manifest)
        counts = {
            "items": conn.execute("SELECT COUNT(*) FROM collaboration_items").fetchone()[0],
            "images": conn.execute("SELECT COUNT(*) FROM collaboration_images").fetchone()[0],
        }
    print(json.dumps({"imported": result, "total": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
