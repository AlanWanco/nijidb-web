"""Read-only API caching, event-loop responsiveness, and calendar semantic parity."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from app.program_api_cache import PublicProgramCache, database_revision

_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main


class ProgramResponseCacheTests(unittest.TestCase):
    def test_conditional_request_skips_builder_and_cached_bytes_are_reused(self):
        cache = PublicProgramCache()
        calls = []

        def build():
            calls.append(True)
            return {"events": []}

        first = cache.response("calendar:a", lambda: "revision-1", build)
        second = cache.response("calendar:a", lambda: "revision-1", build)
        conditional = cache.response("calendar:a", lambda: "revision-1", build, first.etag)
        strong = cache.response("calendar:a", lambda: "revision-1", build, first.etag.removeprefix("W/"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.body, first.body)
        self.assertEqual(conditional.status, 304)
        self.assertIsNone(conditional.body)
        self.assertEqual(strong.status, 304)
        self.assertEqual(cache.response("calendar:b", lambda: "revision-1", build, first.etag).status, 200)
        self.assertEqual(cache.response("calendar:a", lambda: "revision-2", build, first.etag).status, 200)

    def test_cache_is_bounded_and_never_labels_a_mid_build_write(self):
        cache = PublicProgramCache(max_entries=2)
        for key in ("a", "b", "c"):
            cache.response(key, lambda: "stable", lambda: {"events": []})
        self.assertEqual(list(cache.entries), ["b", "c"])
        revisions = iter(("before", "after"))
        changed = cache.response("changed", lambda: next(revisions), lambda: {"events": []})
        self.assertIsNone(changed.etag)
        self.assertNotIn("changed", cache.entries)

    def test_large_responses_and_expired_entries_are_not_retained(self):
        cache = PublicProgramCache(max_body_bytes=8)
        response = cache.response("large", lambda: "stable", lambda: {"events": ["large"]})
        self.assertEqual(response.status, 200)
        self.assertNotIn("large", cache.entries)
        cache = PublicProgramCache(ttl=1)
        with patch("app.program_api_cache.time.monotonic", return_value=100):
            first = cache.response("small", lambda: "stable", lambda: {"events": []})
        with patch("app.program_api_cache.time.monotonic", return_value=102):
            self.assertIsNone(cache.get("small", first.etag))

    def test_revision_observes_minute_rollover_external_write_and_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            self.assertIsNone(database_revision(path, (), minute=1))
            path.write_bytes(b"original")
            first = database_revision(path, (), minute=1)
            self.assertNotEqual(first, database_revision(path, (), minute=2))
            self.assertNotEqual(first, database_revision(path, ("new-public-base",), minute=1))
            path.write_bytes(b"changed!")
            changed = database_revision(path, (), minute=1)
            self.assertNotEqual(first, changed)
            wal = Path(str(path) + "-wal")
            wal.write_bytes(b"wal-1")
            wal_version = database_revision(path, (), minute=1)
            self.assertNotEqual(changed, wal_version)
            wal.write_bytes(b"wal-2")
            self.assertNotEqual(wal_version, database_revision(path, (), minute=1))


class ProgramReadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.patches = [
            patch.object(main, "DB_PATH", root / "nijidb.sqlite3"),
            patch.object(main, "MEDIA_DIR", root / "images"),
            patch.object(main, "BACKUP_DIR", root / "backups"),
            patch.object(main, "R2_ENDPOINT", ""),
            patch.object(main, "R2_PUBLIC_BASE_URL", ""),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        (root / "images").mkdir()
        (root / "backups").mkdir()
        main.init_db()
        main.program_api_cache.clear()
        self.addCleanup(main.program_api_cache.clear)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def seed_program(self, identifier, periods, occurrences, **extras):
        program = main.normalized_program({"title": identifier, "periods": periods, **extras})
        program.update({"id": identifier, "created_at": "fixture", "updated_at": "fixture"})
        with main.db() as conn:
            main.insert_program_row(conn, program)
            for source in occurrences:
                row = main.normalized_occurrence(source)
                row.update({"program_id": identifier, "created_at": "fixture", "updated_at": "fixture"})
                cursor = main.insert_occurrence_row(conn, row)
                main.insert_occurrence_images(conn, cursor.lastrowid, row["images"], "fixture")
        return identifier

    def legacy_calendar(self, start, end):
        programs = main.program_rows(include_occurrences=True)
        return {
            "events": [event for program in programs for event in main.program_calendar_events(program, start, end)],
            "programs": [main.program_summary(program) for program in programs],
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    async def test_calendar_matches_full_detail_path_with_reschedules_ex_deletions_and_links(self):
        today = main.datetime.now(main.JAPAN_TZ).date()
        start = today - timedelta(days=28)
        self.seed_program(
            "weekly",
            [
                {
                    "start_date": start.isoformat(),
                    "frequency": "weekly",
                    "week_interval": 2,
                    "weekday": start.weekday(),
                    "schedule_time": "20:00",
                }
            ],
            [
                {
                    "original_date": start.isoformat(),
                    "original_time": "20:00",
                    "status": "rescheduled",
                    "adjusted_date": (start + timedelta(days=7)).isoformat(),
                    "shift_following_days": 7,
                },
                {
                    "original_date": (start + timedelta(days=14)).isoformat(),
                    "original_time": "20:00",
                    "delivery": "live",
                    "title": "直播特集",
                    "source_url": "https://example.com/source",
                    "mirror_url": "BV1xx411c7mD",
                    "note": "有内容的本期",
                    "guests": ["大西亜玖璃"],
                    "absent_members": ["相良茉優"],
                    "images": [{"url": "https://example.com/photo.png", "alt_text": "返图"}],
                },
                {
                    "original_date": (start + timedelta(days=28)).isoformat(),
                    "original_time": "20:00",
                    "status": "deleted",
                },
                {"original_date": (start + timedelta(days=30)).isoformat(), "original_time": "21:00", "special": "EX"},
            ],
            episode_start=37,
        )
        self.seed_program(
            "manual",
            [
                {"start_date": "2025-01-01", "frequency": "individual"},
                {"start_date": "2026-01-01", "frequency": "individual", "timezone": "UTC"},
            ],
            [
                {"original_date": "2025-01-01", "status": "cancelled"},
                {"original_date": today.isoformat(), "original_time": "19:00", "delivery": "live"},
                {
                    "original_date": (today + timedelta(days=7)).isoformat(),
                    "status": "rescheduled",
                    "adjusted_date": (today + timedelta(days=9)).isoformat(),
                    "is_final": True,
                },
            ],
            auto_generate=False,
        )
        self.seed_program(
            "irregular",
            [
                {
                    "start_date": "2026-01-15",
                    "frequency": "monthly",
                    "monthly_mode": "irregular",
                    "schedule_time": "18:00",
                }
            ],
            [
                {
                    "original_date": today.isoformat(),
                    "generated_date": today.replace(day=1).isoformat(),
                    "original_time": "18:00",
                }
            ],
        )
        end = today + timedelta(days=42)
        expected = self.legacy_calendar(start, end)
        with patch.object(main, "program_occurrence_records", wraps=main.program_occurrence_records) as records:
            actual = main.build_program_calendar(start, end)
        self.assertEqual(records.call_count, 3)
        self.assertEqual(actual, expected)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM program_occurrences").fetchone()[0], 8)

    async def test_summaries_share_one_lightweight_schedule_per_program(self):
        self.seed_program(
            "light",
            [{"start_date": "2025-01-01", "frequency": "individual"}],
            [
                {"original_date": "2025-01-02", "source_url": "https://example.com/one", "guests": ["嘉宾"]},
                {"original_date": "2025-01-03", "special": "EX"},
            ],
            auto_generate=False,
        )
        with (
            patch.object(main, "program_occurrence_records", wraps=main.program_occurrence_records) as records,
            patch.object(main, "safe_program_link", wraps=main.safe_program_link) as links,
        ):
            programs = main.program_rows(include_occurrences=False)
        self.assertEqual(records.call_count, 1)
        self.assertEqual(links.call_count, 1)  # Only the program's official URL.
        self.assertEqual(programs[0]["episode_count"], 1)
        self.assertNotIn("occurrences", programs[0])

    async def test_visible_only_hydration_skips_historical_links_and_preserves_numbering(self):
        self.seed_program(
            "many",
            [{"start_date": "2025-01-01", "frequency": "individual"}],
            [
                {
                    "original_date": (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
                    "source_url": "https://example.com/episode",
                    "guests": ["嘉宾"],
                    "title": "历史期",
                }
                for index in range(100)
            ],
            auto_generate=False,
        )
        with patch.object(main, "safe_program_link", wraps=main.safe_program_link) as links:
            payload = main.build_program_calendar(date(2025, 4, 8), date(2025, 4, 10))
        self.assertEqual(len(payload["events"]), 3)
        self.assertEqual([event["extendedProps"]["episode"] for event in payload["events"]], [98, 99, 100])
        self.assertEqual(links.call_count, 10)  # Program URL plus three links for each visible row.
        self.assertEqual(payload["events"][0]["extendedProps"]["guests"], ["嘉宾"])

    async def test_conditional_calendar_skips_schedule_work_and_external_edit_invalidates(self):
        self.seed_program(
            "etag",
            [{"start_date": "2026-01-01", "frequency": "individual"}],
            [
                {"original_date": "2026-01-02"},
            ],
            auto_generate=False,
        )
        path = "/api/programs/calendar?start=2026-01-01&end=2026-02-01"
        first = await self.client.get(path)
        self.assertEqual(first.status_code, 200)
        tag = first.headers["etag"]
        with patch.object(main, "build_program_calendar", side_effect=AssertionError("must not compute")):
            unchanged = await self.client.get(path, headers={"If-None-Match": tag})
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.content, b"")
        # Deliberately bypass API timestamps/invalidation to model an external importer.
        with sqlite3.connect(main.DB_PATH) as conn:
            conn.execute("UPDATE programs SET title = '外部修改' WHERE id = 'etag'")
        changed = await self.client.get(path, headers={"If-None-Match": tag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["etag"], tag)
        self.assertEqual(changed.json()["programs"][0]["title"], "外部修改")

    async def test_calendar_does_not_block_session_request(self):
        started = threading.Event()
        release = threading.Event()

        def slow_build(start, end):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test release timed out")
            return {"events": [], "programs": [], "start": start.isoformat(), "end": end.isoformat()}

        with patch.object(main, "build_program_calendar", side_effect=slow_build):
            task = asyncio.create_task(self.client.get("/api/programs/calendar?start=2026-01-01&end=2026-02-01"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                response = await asyncio.wait_for(self.client.get("/api/auth/session"), timeout=0.5)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_past_calendar_keeps_restored_anchor_short_window_semantics(self):
        today = main.datetime.now(main.JAPAN_TZ).date()
        start = today - timedelta(days=21)
        original = (today - timedelta(days=10)).isoformat()
        self.seed_program(
            "restored-anchor",
            [
                {
                    "start_date": start.isoformat(),
                    "frequency": "weekly",
                    "week_interval": 2,
                    "weekday": start.weekday(),
                    "schedule_time": "20:00",
                }
            ],
            [
                {
                    "original_date": start.isoformat(),
                    "original_time": "20:00",
                    "status": "rescheduled",
                    "adjusted_date": (start - timedelta(days=7)).isoformat(),
                    "shift_following_days": -7,
                },
                {
                    "original_date": original,
                    "generated_date": (today - timedelta(days=7)).isoformat(),
                    "original_time": "20:00",
                    "source_url": "https://example.com/restored",
                },
            ],
        )
        from_date, to_date = today - timedelta(days=16), today - timedelta(days=8)
        expected = self.legacy_calendar(from_date, to_date)
        actual = main.build_program_calendar(from_date, to_date)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["events"]), 1)
        self.assertTrue(actual["events"][0]["start"].startswith(original))
        self.assertNotIn("_generation_slot", actual["events"][0]["extendedProps"])

    async def test_future_generation_from_shared_range_does_not_leak_into_earlier_stats(self):
        today = main.datetime.now(main.JAPAN_TZ).date()
        start = today - timedelta(days=7)
        self.seed_program(
            "shift",
            [
                {
                    "start_date": start.isoformat(),
                    "frequency": "weekly",
                    "week_interval": 2,
                    "weekday": start.weekday(),
                    "schedule_time": "20:00",
                }
            ],
            [
                {
                    "original_date": start.isoformat(),
                    "original_time": "20:00",
                    "status": "rescheduled",
                    "adjusted_date": (start - timedelta(days=7)).isoformat(),
                    "shift_following_days": -7,
                }
            ],
        )
        program = main.program_rows()[0]
        shared = main.program_statistic_records(program, today + timedelta(days=60))
        self.assertEqual(main.program_episode_count(program, shared), main.program_episode_count(program))
        self.assertEqual(main.program_update_status(program, shared), main.program_update_status(program))
