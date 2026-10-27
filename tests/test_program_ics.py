"""ICS feed for the public program calendar."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main


def unfold(body: str) -> list[str]:
    return body.replace("\r\n ", "").split("\r\n")


def vevents(body: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in unfold(body):
        if line == "BEGIN:VEVENT":
            current = {}
            events.append(current)
        elif line == "END:VEVENT":
            current = None
        elif current is not None:
            name, value = line.split(":", 1)
            current[name] = value
    return events


class ProgramIcsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for item in (
            patch.object(main, "DB_PATH", root / "nijidb.sqlite3"),
            patch.object(main, "MEDIA_DIR", root / "images"),
            patch.object(main, "BACKUP_DIR", root / "backups"),
            patch.object(main, "R2_ENDPOINT", ""),
            patch.object(main, "R2_PUBLIC_BASE_URL", ""),
        ):
            item.start()
            self.addCleanup(item.stop)
        (root / "images").mkdir()
        (root / "backups").mkdir()
        main.init_db()
        main.program_api_cache.clear()
        self.addCleanup(main.program_api_cache.clear)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.today = main.datetime.now(main.JAPAN_TZ).date()
        self.day = self.today - timedelta(days=3)

    def seed_program(self, identifier, occurrences, **extras):
        program = main.normalized_program(
            {"title": identifier, "periods": [{"start_date": "2025-01-01", "frequency": "individual"}], **extras}
        )
        program.update({"id": identifier, "created_at": "fixture", "updated_at": "fixture"})
        with main.db() as conn:
            main.insert_program_row(conn, program)
            for source in occurrences:
                row = main.normalized_occurrence(source)
                row.update({"program_id": identifier, "created_at": "fixture", "updated_at": "fixture"})
                main.insert_occurrence_row(conn, row)

    def seed_fixture(self):
        day = self.day.isoformat()
        self.seed_program(
            "official",
            [
                {
                    "original_date": day,
                    "original_time": "20:00",
                    "delivery": "live",
                    "title": "特集; A, B",
                    "note": "第一行\n第二行" + "很长的备注" * 20,
                    "mirror_url": "BV1xx411c7mD",
                    "guests": ["相良茉優"],
                },
                {
                    "original_date": (self.day + timedelta(days=1)).isoformat(),
                    "status": "rescheduled",
                    "adjusted_date": (self.day + timedelta(days=2)).isoformat(),
                    "adjusted_time": "21:30",
                },
                {"original_date": (self.day + timedelta(days=4)).isoformat(), "status": "cancelled"},
            ],
            category="official",
            people=["大西亜玖璃"],
            auto_generate=False,
        )
        self.seed_program(
            "personal",
            [{"original_date": day, "original_time": "19:00", "absent_members": ["鬼頭明里"]}],
            category="personal",
            people=["鬼頭明里"],
            auto_generate=False,
        )
        self.seed_program(
            "personal-child",
            [{"original_date": day}],
            title="personal",
            parent_id="personal",
            subprogram_name="番外",
            category="personal",
            people=["前田佳織里"],
            auto_generate=False,
        )
        self.seed_program("outsider", [{"original_date": day}], people=["无关主持人"], auto_generate=False)

    async def feed(self, query=""):
        response = await self.client.get(f"/api/programs/calendar.ics{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def summaries(self, query=""):
        return sorted(event["SUMMARY"] for event in vevents((await self.feed(query)).text))

    async def test_default_feed_formats_program_events(self):
        self.seed_fixture()
        response = await self.feed()
        self.assertEqual(response.headers["content-type"], "text/calendar; charset=utf-8")
        self.assertIn("ETag", response.headers)
        body = response.text
        self.assertTrue(body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(body.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", body.replace("\r\n", ""))
        self.assertTrue(all(len(line.encode()) <= 75 for line in body.split("\r\n")))
        events = {event["UID"]: event for event in vevents(body)}
        live = events[f"official/{self.day.isoformat()}/20:00@nijidb"]
        expected_start = main.datetime.combine(self.day, main.time(11, 0)).strftime("%Y%m%dT%H%M%SZ")
        self.assertEqual(live["DTSTART"], expected_start)
        self.assertNotIn("DTEND", live)
        self.assertEqual(live["SUMMARY"], "official · 第1期 · 特集\\; A\\, B")
        self.assertIn("官方节目 · 直播\\n出演：大西亜玖璃\\n嘉宾：相良茉優\\n备注：第一行\\n第二行", live["DESCRIPTION"])
        self.assertIn("搬运地址：https://www.bilibili.com/video/BV1xx411c7mD", live["DESCRIPTION"])
        self.assertEqual(live["CATEGORIES"], "官方节目,直播")
        moved = events[f"official/{(self.day + timedelta(days=1)).isoformat()}/all-day@nijidb"]
        self.assertIn(f"原定：{(self.day + timedelta(days=1)).isoformat()}（东京时间）", moved["DESCRIPTION"])
        cancelled = events[f"official/{(self.day + timedelta(days=4)).isoformat()}/all-day@nijidb"]
        self.assertEqual(cancelled["STATUS"], "CANCELLED")
        child = events[f"personal-child/{self.day.isoformat()}/all-day@nijidb"]
        self.assertEqual(child["DTSTART;VALUE=DATE"], self.day.strftime("%Y%m%d"))
        self.assertEqual(child["DTEND;VALUE=DATE"], (self.day + timedelta(days=1)).strftime("%Y%m%d"))
        # Programs without any Nijigasaki member stay hidden, as on the calendar page.
        self.assertFalse(any(uid.startswith("outsider/") for uid in events))

    async def test_filters_match_calendar_page_query(self):
        self.seed_fixture()
        everything = await self.summaries()
        self.assertEqual(len(everything), 5)
        self.assertEqual(await self.summaries("?cast=相良茉优"), ["official · 第1期 · 特集\\; A\\, B"])
        # The member is absent from that episode, so only the subprogram with another member remains.
        self.assertEqual(await self.summaries("?cast=鬼頭明里"), [])
        self.assertEqual(await self.summaries("?cast=鬼頭明里,前田佳織里"), ["personal · 番外 · 第1期"])
        self.assertEqual(await self.summaries("?category=personal"), ["personal · 番外 · 第1期", "personal · 第1期"])
        self.assertEqual(await self.summaries("?category=official,personal"), everything)
        self.assertEqual(await self.summaries("?cast=" + ",".join(main.NIJIGASAKI_CAST_NAMES)), everything)
        self.assertEqual(await self.summaries("?delivery=live"), ["official · 第1期 · 特集\\; A\\, B"])
        self.assertEqual(await self.summaries("?delivery=none"), [])
        self.assertEqual(
            await self.summaries("?delivery=recorded&category=official"),
            ["official · 原定第3期 · 已取消", "official · 第2期"],
        )

    async def test_program_filter_includes_subprograms_and_names_calendar(self):
        self.seed_fixture()
        response = await self.feed("?program=personal")
        self.assertIn("X-WR-CALNAME:personal · Nijigasaki DB", unfold(response.text))
        self.assertEqual(await self.summaries("?program=personal"), ["personal · 番外 · 第1期", "personal · 第1期"])
        self.assertEqual(await self.summaries("?program=personal-child"), ["personal · 番外 · 第1期"])
        # Explicitly selected programs are shown even without a Nijigasaki member.
        self.assertEqual(await self.summaries("?program=outsider"), ["outsider · 第1期"])
        self.assertEqual(await self.summaries("?program=missing"), [])

    async def test_invalid_parameters_are_rejected(self):
        for query in (
            "?cast=不存在",
            "?category=unknown",
            "?delivery=tv",
            "?start=2026-01-01&end=2027-02-01",
            "?start=2026-02-01&end=2026-01-01",
            "?start=bad",
        ):
            with self.subTest(query=query):
                response = await self.client.get(f"/api/programs/calendar.ics{query}")
                self.assertEqual(response.status_code, 400)

    async def test_explicit_range_and_conditional_request(self):
        self.seed_fixture()
        start = self.day + timedelta(days=1)
        query = f"?start={start.isoformat()}&end={(start + timedelta(days=2)).isoformat()}"
        self.assertEqual(await self.summaries(query), ["official · 第2期"])
        first = await self.feed(query)
        with patch.object(main, "build_program_ics", side_effect=AssertionError("must not rebuild")):
            unchanged = await self.client.get(
                f"/api/programs/calendar.ics{query}", headers={"If-None-Match": first.headers["etag"]}
            )
        self.assertEqual(unchanged.status_code, 304)

    async def test_eventernote_events_follow_cast_filter_and_skip_cache(self):
        self.seed_fixture()
        day = self.day.isoformat()
        payload = {
            "items": [
                {
                    "id": 42,
                    "title": "Live; Day 1",
                    "event_date": day,
                    "start_time": "18:00",
                    "end_time": "20:30",
                    "raw_time_text": "開場17:00 開演18:00",
                    "place_name": "東京ガーデンシアター",
                    "source_url": "https://www.eventernote.com/events/42",
                    "performers": [{"actor": {"name": "相良茉優"}}],
                },
                {"id": 43, "title": "Other", "event_date": day, "performers": [{"actor": {"name": "鬼頭明里"}}]},
                {"id": 44, "title": "", "event_date": day},
            ]
        }
        fetch = AsyncMock(return_value=payload)
        with patch.object(main, "fetch_eventernote_events", fetch):
            response = await self.feed("?delivery=event&cast=相良茉優")
            with_programs = await self.feed("?delivery=live,event")
            await self.feed("?delivery=event&program=official")
        self.assertEqual(fetch.await_count, 2)
        self.assertNotIn("ETag", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        [event] = vevents(response.text)
        self.assertEqual(event["UID"], "eventernote-42@nijidb")
        self.assertEqual(event["SUMMARY"], "Live\\; Day 1")
        self.assertEqual(event["DTSTART"], main.datetime.combine(self.day, main.time(9, 0)).strftime("%Y%m%dT%H%M%SZ"))
        self.assertEqual(event["DTEND"], main.datetime.combine(self.day, main.time(11, 30)).strftime("%Y%m%dT%H%M%SZ"))
        self.assertEqual(event["LOCATION"], "東京ガーデンシアター")
        self.assertEqual(event["URL"], "https://www.eventernote.com/events/42")
        self.assertEqual(
            sorted(event["SUMMARY"] for event in vevents(with_programs.text)),
            ["Live\\; Day 1", "Other", "official · 第1期 · 特集\\; A\\, B"],
        )


if __name__ == "__main__":
    unittest.main()
