"""Offline regressions for program period scheduling."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch


_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main


class ProgramPeriodSchedulingTests(unittest.TestCase):
    def test_individual_period_defaults_to_disabled(self):
        start = date(2026, 1, 1)
        individual = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "individual"},
            start,
            None,
        )
        weekly = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "weekly"},
            start,
            None,
        )
        monthly = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "monthly", "week_index": 1},
            start,
            None,
        )
        single = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "single"},
            start,
            None,
        )
        self.assertFalse(individual["auto_generate"])
        self.assertFalse(single["auto_generate"])
        self.assertTrue(weekly["auto_generate"])
        self.assertTrue(monthly["auto_generate"])
        self.assertTrue(
            main.normalized_period(
                {"start_date": start.isoformat(), "frequency": "individual", "auto_generate": True},
                start,
                None,
            )["auto_generate"]
        )

    def test_period_auto_generation_can_be_disabled_without_removing_manual_episode(self):
        today = datetime_today()
        start = date(today.year, today.month, 1)
        range_end = today + timedelta(days=100)
        period = {
            "start_date": start.isoformat(),
            "end_date": range_end.isoformat(),
            "frequency": "individual",
            "auto_generate": False,
            "schedule_time": "20:00",
            "timezone": "Asia/Tokyo",
        }
        program = {
            "id": "program-test",
            "title": "测试节目",
            "start_date": start.isoformat(),
            "end_date": range_end.isoformat(),
            "auto_generate": True,
            "delivery": "recorded",
            "periods": [period],
            "occurrences": [],
        }
        self.assertEqual(main.program_occurrence_records(program, start, range_end), [])

        program["occurrences"] = [{"original_date": (start + timedelta(days=3)).isoformat(), "original_time": "20:00"}]
        manual_records = main.program_occurrence_records(program, start, range_end)
        self.assertEqual(len(manual_records), 1)
        self.assertTrue(manual_records[0]["manual"])

        period["auto_generate"] = True
        generated_records = main.program_occurrence_records(program, start, range_end)
        self.assertGreaterEqual(len(generated_records), 3)
        self.assertTrue(any(record["generated"] for record in generated_records))
        self.assertTrue(any(record["manual"] for record in generated_records))

    def test_single_period_only_generates_start_date_when_enabled(self):
        today = datetime_today()
        start = date(today.year, today.month, 1)
        period = {
            "start_date": start.isoformat(),
            "end_date": start.isoformat(),
            "frequency": "single",
            "auto_generate": False,
            "schedule_time": "20:00",
            "timezone": "Asia/Tokyo",
        }
        program = {
            "id": "single-program-test",
            "start_date": start.isoformat(),
            "end_date": "",
            "auto_generate": True,
            "delivery": "recorded",
            "periods": [period],
            "occurrences": [],
        }
        self.assertEqual(main.program_occurrence_records(program, start, start), [])
        period["auto_generate"] = True
        records = main.program_occurrence_records(program, start, start)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["original_date"], start.isoformat())

    def test_period_auto_generate_is_persisted_in_period_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE program_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                program_id TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL DEFAULT '',
                frequency TEXT NOT NULL DEFAULT 'weekly',
                auto_generate INTEGER NOT NULL DEFAULT 1,
                week_interval INTEGER NOT NULL DEFAULT 1,
                week_index INTEGER NOT NULL DEFAULT 0,
                weekday INTEGER NOT NULL DEFAULT 0,
                schedule_time TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        main.replace_program_periods(
            conn,
            "program-test",
            [
                {
                    "start_date": "2026-01-01",
                    "end_date": "",
                    "frequency": "individual",
                    "auto_generate": False,
                    "week_interval": 1,
                    "week_index": 0,
                    "weekday": 0,
                    "schedule_time": "",
                    "timezone": "Asia/Tokyo",
                }
            ],
            "2026-01-01T00:00:00+00:00",
        )
        row = conn.execute(
            "SELECT auto_generate FROM program_periods WHERE program_id = ?", ("program-test",)
        ).fetchone()
        self.assertEqual(row["auto_generate"], 0)
        conn.close()


def datetime_today() -> date:
    return main.datetime.now(main.JAPAN_TZ).date()


if __name__ == "__main__":
    unittest.main()
