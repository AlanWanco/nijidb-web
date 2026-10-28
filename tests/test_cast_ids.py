"""Keep the ICS cast ids in app/main.py and frontend/src/programCast.js identical.

The subscription feed keys cast by numeric id, so the two tables must agree on
both the id-to-name mapping and the accepted name aliases.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_import_dir = tempfile.TemporaryDirectory()
with patch.dict(
    os.environ,
    {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"},
):
    from app import main

ROOT = Path(__file__).resolve().parent.parent
CAST_SOURCE = ROOT / "frontend" / "src" / "programCast.js"

ENTRY_PATTERN = re.compile(
    r'\{\s*id:\s*(\d+),\s*name:\s*"([^"]+)",\s*aliases:\s*\[([^\]]*)\],\s*color:\s*"#[0-9a-fA-F]{3,6}"\s*\}'
)
ALIAS_PATTERN = re.compile(r'"([^"]+)"')


def parse_cast_source(text: str) -> dict[int, tuple[str, tuple[str, ...]]]:
    """Read the frontend member table without running JavaScript."""
    entries: dict[int, tuple[str, tuple[str, ...]]] = {}
    for match in ENTRY_PATTERN.finditer(text):
        cast_id = int(match.group(1))
        if cast_id in entries:
            raise ValueError(f"frontend cast id {cast_id} is declared twice")
        entries[cast_id] = (match.group(2), tuple(ALIAS_PATTERN.findall(match.group(3))))
    return entries


def cast_mismatches(
    frontend: dict[int, tuple[str, tuple[str, ...]]],
    backend: dict[int, str],
) -> list[str]:
    problems = []
    for cast_id in sorted(set(frontend) - set(backend)):
        problems.append(f"编号 {cast_id} 只在 {CAST_SOURCE.name} 里：{frontend[cast_id][0]}")
    for cast_id in sorted(set(backend) - set(frontend)):
        problems.append(f"编号 {cast_id} 只在 app/main.py 里：{backend[cast_id]}")
    for cast_id in sorted(set(frontend) & set(backend)):
        if frontend[cast_id][0] != backend[cast_id]:
            problems.append(
                f"编号 {cast_id} 名字不一致：{CAST_SOURCE.name}={frontend[cast_id][0]}，main.py={backend[cast_id]}"
            )
    names = list(backend.values())
    for name in sorted({item for item in names if names.count(item) > 1}):
        problems.append(f"app/main.py 里 {name} 占用了多个编号")
    return problems


def alias_mismatches(
    frontend: dict[int, tuple[str, tuple[str, ...]]],
    backend_aliases: dict[str, str],
) -> list[str]:
    expected = {
        alias: name
        for name, aliases in frontend.values()
        for alias in aliases
    }
    problems = []
    for alias in sorted(set(expected) - set(backend_aliases)):
        problems.append(f"别名 {alias} 只在 {CAST_SOURCE.name} 里")
    for alias in sorted(set(backend_aliases) - set(expected)):
        problems.append(f"别名 {alias} 只在 app/main.py 里")
    for alias in sorted(set(expected) & set(backend_aliases)):
        if expected[alias] != backend_aliases[alias]:
            problems.append(
                f"别名 {alias} 指向不一致：{CAST_SOURCE.name}={expected[alias]}，main.py={backend_aliases[alias]}"
            )
    return problems


class CastIdConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.source = CAST_SOURCE.read_text(encoding="utf-8")
        self.frontend = parse_cast_source(self.source)

    def test_frontend_table_is_parseable(self):
        self.assertEqual(len(self.frontend), len(main.NIJIGASAKI_CAST_BY_ID))
        self.assertIn(1, self.frontend)
        self.assertIn(14, self.frontend)

    def test_cast_ids_match_backend(self):
        self.assertEqual(cast_mismatches(self.frontend, main.NIJIGASAKI_CAST_BY_ID), [])

    def test_cast_aliases_match_backend(self):
        self.assertEqual(alias_mismatches(self.frontend, main.NIJIGASAKI_NAME_ALIASES), [])

    def test_detects_a_renumbered_member(self):
        drifted = self.source.replace(
            '{ id: 14, name: "楠木ともり"', '{ id: 7, name: "楠木ともり"', 1
        ).replace('{ id: 7, name: "林鼓子"', '{ id: 15, name: "林鼓子"', 1)
        problems = cast_mismatches(parse_cast_source(drifted), main.NIJIGASAKI_CAST_BY_ID)
        self.assertTrue(problems, "编号改动没有被发现")

    def test_detects_a_renamed_member(self):
        drifted = self.source.replace('name: "田中ちえ美"', 'name: "田中智惠美"', 1)
        problems = cast_mismatches(parse_cast_source(drifted), main.NIJIGASAKI_CAST_BY_ID)
        self.assertTrue(problems, "名字改动没有被发现")

    def test_detects_an_alias_that_drifts(self):
        drifted = self.source.replace('aliases: ["田中千惠美"]', 'aliases: ["田中智惠美"]', 1)
        problems = alias_mismatches(parse_cast_source(drifted), main.NIJIGASAKI_NAME_ALIASES)
        self.assertTrue(problems, "别名改动没有被发现")


if __name__ == "__main__":
    unittest.main()
