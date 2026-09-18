"""Offline regressions for program period scheduling."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main


class ProgramPeriodSchedulingTests(unittest.TestCase):
    def test_sync_exception_label_identifies_type_and_http_status(self):
        request = httpx.Request("POST", "https://example.com/cd_detail.php")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("blocked", request=request, response=response)
        self.assertEqual(main.sync_exception_label(error), "HTTPStatusError HTTP 403")
        self.assertEqual(main.sync_exception_label(httpx.ReadTimeout("")), "ReadTimeout")

    def test_period_defaults_separate_monthly_irregular_and_individual(self):
        start = date(2026, 1, 1)
        individual = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "individual", "schedule_time": "20:00"},
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
        irregular = main.normalized_period(
            {
                "start_date": start.isoformat(),
                "frequency": "monthly",
                "monthly_mode": "irregular",
                "schedule_time": "20:00",
            },
            start,
            None,
        )
        single = main.normalized_period(
            {"start_date": start.isoformat(), "frequency": "single", "auto_generate": False},
            start,
            None,
        )
        self.assertFalse(individual["auto_generate"])
        self.assertEqual(individual["schedule_time"], "20:00")
        self.assertTrue(single["auto_generate"])
        self.assertTrue(weekly["auto_generate"])
        self.assertTrue(monthly["auto_generate"])
        self.assertEqual(irregular["monthly_mode"], "irregular")
        self.assertEqual(irregular["week_index"], 0)
        self.assertEqual(irregular["weekday"], 0)
        self.assertEqual(irregular["schedule_time"], "20:00")
        self.assertFalse(
            main.normalized_period(
                {"start_date": start.isoformat(), "frequency": "individual", "auto_generate": True},
                start,
                None,
            )["auto_generate"]
        )

    def test_individual_period_is_manual_even_if_legacy_flag_is_true(self):
        today = datetime_today()
        start = date(today.year, today.month, 1)
        range_end = today + timedelta(days=100)
        period = {
            "start_date": start.isoformat(),
            "end_date": range_end.isoformat(),
            "frequency": "individual",
            "auto_generate": True,
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

    def test_irregular_monthly_period_uses_first_of_each_month_as_placeholder(self):
        start = date(2026, 1, 1)
        end = date(2026, 4, 30)
        period = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "frequency": "monthly",
            "monthly_mode": "irregular",
            "auto_generate": True,
            "schedule_time": "20:00",
            "timezone": "Asia/Tokyo",
        }
        dates = main.period_recurring_dates(period, end)
        self.assertEqual(dates, [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)])
        program = {
            "id": "irregular-program-test",
            "title": "无规律月更测试节目",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "auto_generate": True,
            "delivery": "recorded",
            "periods": [period],
            "occurrences": [],
        }
        records = main.program_occurrence_records(program, start, end)
        self.assertEqual([record["original_time"] for record in records], ["20:00"] * 4)

    def test_single_period_generates_start_date_by_default(self):
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
        records = main.program_occurrence_records(program, start, start)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["generated"])
        self.assertEqual(records[0]["original_date"], start.isoformat())
        self.assertEqual(records[0]["original_time"], "20:00")

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
                monthly_mode TEXT NOT NULL DEFAULT 'week',
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
                    "monthly_mode": "week",
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


class IndividualProgramApiTests(unittest.IsolatedAsyncioTestCase):
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
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.client.cookies.set("nijidb_admin", main.admin_cookie_value(main.settings()["admin_password_hash"]))

    async def test_individual_program_seeds_first_occurrence_regardless_of_auto_generation(self):
        start = main.datetime.now(main.JAPAN_TZ).date() + timedelta(days=7)
        for auto_generate in (False, True):
            payload = {
                "title": f"逐期首集测试节目-{auto_generate}",
                "auto_generate": auto_generate,
                "episode_start": 37,
                "periods": [{
                    "start_date": start.isoformat(),
                    "frequency": "individual",
                    "schedule_time": "20:00",
                    "timezone": "Asia/Tokyo",
                }],
            }
            created = await self.client.post("/api/admin/programs", json=payload)
            self.assertEqual(created.status_code, 200)
            program_id = created.json()["program"]["id"]

            listing = await self.client.get(f"/api/admin/programs/{program_id}/occurrences")
            self.assertEqual(listing.status_code, 200)
            occurrences = listing.json()["occurrences"]
            self.assertEqual(len(occurrences), 1)
            first = occurrences[0]
            self.assertEqual(first["original_date"], start.isoformat())
            self.assertEqual(first["original_time"], "20:00")
            self.assertEqual(first["episode"], 37)
            self.assertFalse(first["generated"])
            self.assertTrue(first["individual"])
            self.assertTrue(first["manual"])

            with main.db() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM program_occurrences WHERE program_id = ?",
                        (program_id,),
                    ).fetchone()[0],
                    1,
                )

            payload["auto_generate"] = not auto_generate
            updated = await self.client.patch(f"/api/admin/programs/{program_id}", json=payload)
            self.assertEqual(updated.status_code, 200)
            listing_after_update = await self.client.get(f"/api/admin/programs/{program_id}/occurrences")
            self.assertEqual(len(listing_after_update.json()["occurrences"]), 1)


class MusicSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_deferred_detail_preserves_existing_record(self):
        class FakeResponse:
            text = """
                <ul class="list">
                  <li><a href="#cd01_5601">列表标题</a></li>
                </ul>
                <div class="box" id="cd01_5601"><p>不完整列表资料</p></div>
            """

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, *args, **kwargs):
                return FakeResponse()

            async def post(self, *args, **kwargs):
                raise httpx.ReadTimeout("")

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "nijidb.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """CREATE TABLE releases (
                    id TEXT PRIMARY KEY, title TEXT, subtitle TEXT, artist TEXT, release_date TEXT, price TEXT,
                    cover_url TEXT, detail_html TEXT, source_url TEXT, fingerprint TEXT, updated_at TEXT,
                    position INTEGER, tracks_json TEXT, spec_json TEXT, extras_json TEXT
                )"""
            )
            connection.execute(
                """INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "cd01_5601",
                    "完整资料标题",
                    "完整副标题",
                    "虹ヶ咲学園スクールアイドル同好会",
                    "2026年2月4日(水)",
                    "￥3,850",
                    "https://cdn.example.com/cover.jpg",
                    "<article>完整详情</article>",
                    "https://example.com#cd01_5601",
                    "fingerprint",
                    "2026-09-13T00:00:00+00:00",
                    1,
                    '[{"title":"完整曲目"}]',
                    '{"品番":"LACA-19159"}',
                    "[]",
                ),
            )
            connection.commit()
            connection.close()
            with patch.object(main, "DB_PATH", database_path), patch.object(main, "SOURCE_URL", "https://example.com/cd.php"), patch.object(main.httpx, "AsyncClient", FakeClient):
                records = await main.scrape()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "完整资料标题")
        self.assertEqual(records[0]["detail_html"], "<article>完整详情</article>")
        self.assertEqual(records[0]["tracks_json"], '[{"title":"完整曲目"}]')


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
        content = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (360).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
            + b"program-photo"
        )
        with (
            patch.object(main, "r2_is_configured", return_value=True),
            patch.object(main, "r2_upload_is_configured", return_value=True),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "fetch_public_image_bytes", new_callable=AsyncMock, return_value=(content + b"direct", "image/png")),
            patch.object(main, "upload_image_to_r2"),
        ):
            direct = await self.client.post(endpoint, json={"url": "https://cdn.example.com/live.jpg", "alt_text": "直链"})
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.json()["image"]["kind"], "upload")
        self.assertTrue(direct.json()["image"]["url"].startswith("https://images.example.test/"))

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
        self.assertTrue(images[0]["url"].startswith("https://images.example.test/"))

        deleted = await self.client.delete(
            f"{endpoint}/{uploaded_image['id']}"
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len(deleted.json()["occurrence"]["images"]), 1)
        self.assertFalse((self.media_directory / uploaded_image["local_path"]).exists())


class MusicCoverApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media_directory = Path(self.directory.name) / "images"
        self.media_directory.mkdir(parents=True)
        self.illustration_directory = Path(self.directory.name) / "illustrations"
        self.illustration_directory.mkdir(parents=True)
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.directory.name) / "nijidb.sqlite3"),
            patch.object(main, "MEDIA_DIR", self.media_directory),
            patch.object(main, "ILLUSTRATION_RUNTIME_DIR", self.illustration_directory),
            patch.object(main, "R2_ENDPOINT", ""),
            patch.object(main, "R2_ACCESS_KEY_ID", ""),
            patch.object(main, "R2_SECRET_ACCESS_KEY", ""),
            patch.object(main, "R2_PUBLIC_BASE_URL", ""),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        main.init_db()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO releases
                   (id, title, detail_html, source_url, fingerprint, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("cd01_9998", "测试音乐发行", "", "https://example.com", "old", "2026-01-01T00:00:00+00:00"),
            )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.client.cookies.set("nijidb_admin", main.admin_cookie_value(main.settings()["admin_password_hash"]))

    async def test_collabo_direct_url_upload_archives_to_r2(self):
        content = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (360).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
            + b"collabo-cover"
        )
        with (
            patch.object(main, "r2_is_configured", return_value=True),
            patch.object(main, "r2_upload_is_configured", return_value=True),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "fetch_public_image_bytes", new_callable=AsyncMock, return_value=(content, "image/png")),
            patch.object(main, "upload_image_to_r2"),
        ):
            response = await self.client.post(
                "/api/admin/collabo/assets",
                json={"url": "https://cdn.example.com/collabo.png"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()["images"][0]
        self.assertEqual(image["source_url"], "https://cdn.example.com/collabo.png")
        self.assertTrue(image["url"].startswith("https://images.example.test/"))

    async def test_direct_url_and_binary_cover_upload_update_release(self):
        content = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (360).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
            + b"release-cover"
        )
        with (
            patch.object(main, "r2_is_configured", return_value=True),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "fetch_public_image_bytes", new_callable=AsyncMock, return_value=(content, "image/png")),
            patch.object(main, "upload_cover_to_r2"),
        ):
            direct = await self.client.post(
                "/api/admin/releases/cd01_9998/cover",
                json={"url": "https://cdn.example.com/cover.jpg"},
            )
            self.assertEqual(direct.status_code, 200)
            self.assertTrue(direct.json()["release"]["cover_url"].startswith("https://images.example.test/"))
            binary = await self.client.post(
                "/api/admin/releases/cd01_9998/cover",
                content=content,
                headers={"Content-Type": "image/png", "X-Filename": "cover.png"},
            )
        self.assertEqual(binary.status_code, 200)
        with main.db() as conn:
            self.assertTrue(conn.execute("SELECT cover_url FROM releases WHERE id = ?", ("cd01_9998",)).fetchone()[0])


def datetime_today() -> date:
    return main.datetime.now(main.JAPAN_TZ).date()


if __name__ == "__main__":
    unittest.main()
