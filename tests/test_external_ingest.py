"""Offline regressions for the keyed external ingestion APIs."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main


class ExternalIngestApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.directory.name) / "test.sqlite3"),
            patch.object(main, "COLLABORATION_INDEX_PATHS", ()),
            patch.object(main, "ILLUSTRATION_MANIFEST_PATH", Path(self.directory.name) / "none"),
            patch.object(main, "sync_lock", asyncio.Lock()),
            patch.object(main, "news_run_lock", asyncio.Lock()),
            patch.object(main, "official_site_health_lock", asyncio.Lock()),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "R2_IMAGE_PREFIX", "images"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        main.init_db()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def login(self):
        self.client.cookies.set("nijidb_admin", main.admin_cookie_value(main.settings()["admin_password_hash"]))

    @staticmethod
    def music_payload(title: str = "外部音乐"):
        return {
            "release": {
                "id": "cd_external_001",
                "title": title,
                "artist": "虹ヶ咲学園スクールアイドル同好会",
                "release_date": "2026-09-20",
                "cover_url": "https://images.example.test/images/music/cd_external_001.jpg",
                "detail_html": "<p>详情</p>",
                "source_url": "https://www.example.com/music/cd_external_001",
                "tracks": [{"disc": "", "number": 1, "title": "曲目", "credits": {}}],
                "specs": {"规格": "CD"},
                "extras": [],
            }
        }

    @staticmethod
    def program_payload(title: str = "外部节目"):
        return {
            "program": {
                "id": "program_external_001",
                "title": title,
                "category": "personal",
                "format": "video",
                "platform": "network",
                "delivery": "recorded",
                "periods": [
                    {
                        "start_date": "2026-09-20",
                        "end_date": "",
                        "frequency": "individual",
                        "schedule_time": "20:00",
                        "timezone": "Asia/Tokyo",
                    }
                ],
                "people": ["成员姓名"],
                "official_url": "https://www.example.com/program_external_001",
                "description": "节目简介",
            },
            "occurrences": [
                {
                    "original_date": "2026-09-20",
                    "original_time": "20:00",
                    "title": "第一期",
                    "status": "scheduled",
                    "images": [],
                }
            ],
            "subprograms": [],
        }

    async def test_each_resource_uses_its_own_key_and_music_is_idempotent(self):
        with patch.dict(os.environ, {"NIJIDB_MUSIC_INGEST_API_KEY": "music-test-key"}):
            missing = await self.client.post("/api/ingest/music", json=self.music_payload())
            self.assertEqual(missing.status_code, 401)
            response = await self.client.post(
                "/api/ingest/music",
                json=self.music_payload(),
                headers={"X-Nijidb-API-Key": "music-test-key"},
            )
            repeated = await self.client.post(
                "/api/ingest/music",
                json=self.music_payload(),
                headers={"X-Nijidb-API-Key": "music-test-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["created"])
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json()["created"])
        self.assertFalse(repeated.json()["changed"])
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 1)

    async def test_program_api_keeps_source_id_and_replaces_full_snapshot(self):
        with patch.dict(os.environ, {"NIJIDB_PROGRAM_INGEST_API_KEY": "program-test-key"}):
            response = await self.client.post(
                "/api/ingest/program",
                json=self.program_payload(),
                headers={"X-Nijidb-API-Key": "program-test-key"},
            )
            updated = await self.client.post(
                "/api/ingest/program",
                json=self.program_payload("更新后的节目"),
                headers={"X-Nijidb-API-Key": "program-test-key"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["created"])
        self.assertEqual(response.json()["id"], "program_external_001")
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(updated.json()["created"])
        with main.db() as conn:
            program = conn.execute("SELECT title FROM programs WHERE id = ?", ("program_external_001",)).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM program_occurrences WHERE program_id = ?", ("program_external_001",)
            ).fetchone()[0]
        self.assertEqual(program["title"], "更新后的节目")
        self.assertEqual(count, 1)

    async def test_collabo_api_accepts_r2_image_and_stable_source_id(self):
        payload = {
            "item": {
                "source_id": "campaign_external_001",
                "title": "外部联动",
                "date": "2026-09-20",
                "tags": ["ayumu"],
                "collection_status": "complete",
                "review_status": "approved",
                "images": [
                    {
                        "url": "https://www.example.com/source/campaign.jpg",
                        "source_url": "https://www.example.com/source/campaign.jpg",
                        "public_url": "https://images.example.test/images/collabo/campaign.jpg",
                        "alt": "联动立绘",
                    }
                ],
            }
        }
        with patch.dict(os.environ, {"NIJIDB_COLLABO_INGEST_API_KEY": "collabo-test-key"}):
            response = await self.client.post(
                "/api/ingest/collabo",
                json=payload,
                headers={"X-Nijidb-API-Key": "collabo-test-key"},
            )
            payload["item"]["title"] = "更新后的联动"
            updated = await self.client.post(
                "/api/ingest/collabo",
                json=payload,
                headers={"X-Nijidb-API-Key": "collabo-test-key"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["created"])
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(updated.json()["created"])
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collaboration_items").fetchone()[0], 1)
            item = conn.execute("SELECT title FROM collaboration_items").fetchone()
            image = conn.execute("SELECT public_url FROM collaboration_images").fetchone()
        self.assertEqual(item["title"], "更新后的联动")
        self.assertEqual(image["public_url"], payload["item"]["images"][0]["public_url"])

    async def test_api_docs_are_admin_only_and_do_not_return_keys(self):
        self.assertEqual((await self.client.get("/api/admin/external-api-docs")).status_code, 401)
        self.login()
        response = await self.client.get("/api/admin/external-api-docs")
        self.assertEqual(response.status_code, 200)
        docs = response.json()
        self.assertEqual([item["id"] for item in docs["resources"]], ["music", "program", "collabo", "news"])
        self.assertEqual(docs["header"], "X-Nijidb-API-Key")
        self.assertTrue(all("key" not in item for item in docs["resources"]))

    async def test_collabo_api_rejects_local_paths(self):
        payload = {
            "item": {
                "source_id": "local-path-test",
                "title": "不应写入",
                "date": "2026-09-20",
                "images": [{"path": "images/local.jpg"}],
            }
        }
        with patch.dict(os.environ, {"NIJIDB_COLLABO_INGEST_API_KEY": "collabo-test-key"}):
            response = await self.client.post(
                "/api/ingest/collabo",
                json=payload,
                headers={"X-Nijidb-API-Key": "collabo-test-key"},
            )
        self.assertEqual(response.status_code, 400)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collaboration_items").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
