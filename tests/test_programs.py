"""Offline regressions for program period scheduling."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx


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

    def test_occurrence_images_are_normalized_and_carried_to_records(self):
        values = main.normalized_occurrence(
            {
                "original_date": "2026-01-07",
                "images": [
                    {"url": "https://cdn.example.com/photo.jpg", "alt_text": "现场返图"},
                    "https://cdn.example.com/photo-2.jpg",
                ],
            }
        )
        self.assertEqual(
            values["images"],
            [
                {"url": "https://cdn.example.com/photo.jpg", "alt_text": "现场返图"},
                {"url": "https://cdn.example.com/photo-2.jpg", "alt_text": ""},
            ],
        )
        record = main.occurrence_record(
            {"title": "测试节目", "delivery": "recorded"},
            date(2026, 1, 7),
            values,
            schedule_time="20:00",
        )
        self.assertEqual([image["url"] for image in record["images"]], [
            "https://cdn.example.com/photo.jpg",
            "https://cdn.example.com/photo-2.jpg",
        ])

    def test_occurrence_image_url_must_be_external_http_url(self):
        with self.assertRaises(ValueError):
            main.normalized_occurrence(
                {"original_date": "2026-01-07", "images": [{"url": "/media/programs/photo.jpg"}]}
            )

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


class ProgramImageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media_directory = Path(self.directory.name) / "images"
        self.media_directory.mkdir(parents=True)
        self.backup_directory = Path(self.directory.name) / "backups"
        self.backup_directory.mkdir(parents=True)
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.directory.name) / "nijidb.sqlite3"),
            patch.object(main, "MEDIA_DIR", self.media_directory),
            patch.object(main, "BACKUP_DIR", self.backup_directory),
            patch.object(main, "R2_ENDPOINT", ""),
            patch.object(main, "R2_ACCESS_KEY_ID", ""),
            patch.object(main, "R2_SECRET_ACCESS_KEY", ""),
            patch.object(main, "R2_PUBLIC_BASE_URL", ""),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        main.init_db()
        now = "2026-01-01T00:00:00+00:00"
        program = main.normalized_program({
            "title": "返图测试节目",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "periods": [{
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "frequency": "weekly",
                "schedule_time": "20:00",
                "timezone": "Asia/Tokyo",
            }],
        })
        program.update({"id": "program-image-test", "created_at": now, "updated_at": now})
        with main.db() as conn:
            main.insert_program_row(conn, program)
            occurrence = main.normalized_occurrence({
                "original_date": "2026-01-07",
                "original_time": "20:00",
            })
            occurrence.update({"program_id": program["id"], "created_at": now, "updated_at": now})
            self.occurrence_id = main.insert_occurrence_row(conn, occurrence).lastrowid
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.client.cookies.set("nijidb_admin", main.admin_cookie_value(main.settings()["admin_password_hash"]))

    async def test_direct_and_uploaded_images_are_returned_and_deletable(self):
        endpoint = f"/api/admin/programs/program-image-test/occurrences/{self.occurrence_id}/images"
        direct = await self.client.post(endpoint, json={"url": "https://cdn.example.com/live.jpg", "alt_text": "直链"})
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.json()["image"]["kind"], "external")
        self.assertEqual(direct.json()["image"]["url"], "https://cdn.example.com/live.jpg")

        content = b"\x89PNG\r\n\x1a\nprogram-photo"
        uploaded = await self.client.post(
            endpoint,
            content=content,
            headers={"Content-Type": "image/png", "X-Filename": "photo.png"},
        )
        self.assertEqual(uploaded.status_code, 200)
        uploaded_image = uploaded.json()["image"]
        self.assertEqual(uploaded_image["kind"], "upload")
        self.assertEqual(uploaded_image["url"], f"/media/{uploaded_image['local_path']}")
        self.assertTrue((self.media_directory / uploaded_image["local_path"]).is_file())

        public = await self.client.get("/api/programs/program-image-test/occurrences")
        public_occurrences = public.json()["occurrences"]
        image_occurrence = next(item for item in public_occurrences if item["id"] == self.occurrence_id)
        images = image_occurrence["images"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["url"], "https://cdn.example.com/live.jpg")

        deleted = await self.client.delete(
            f"{endpoint}/{uploaded_image['id']}"
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len(deleted.json()["occurrence"]["images"]), 1)
        self.assertFalse((self.media_directory / uploaded_image["local_path"]).exists())



def datetime_today() -> date:
    return main.datetime.now(main.JAPAN_TZ).date()


if __name__ == "__main__":
    unittest.main()
