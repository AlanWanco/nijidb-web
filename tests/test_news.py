"""Offline regressions: temporary SQLite only; no web crawls or runtime data writes."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.news import (
    ensure_news_schema,
    news_id,
    parse_local_markdown,
    parse_topic_detail,
    upsert_news_record,
)
from app.news_fetch import fetch_news_page

# main creates its runtime directories at import time.
_import_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _import_dir.name, "ADMIN_SECRET": "test-only", "ADMIN_PASSWORD": "test-only"}):
    from app import main

URL = "https://www.lovelive-anime.jp/news/01_123.html"
HTML = '<main><h2>新しいお知らせ</h2><p>2026.09.11</p><h3>詳細</h3><p><strong>更新本文</strong> <a href="/link">リンク</a></p><ul><li>項目</li></ul></main>'


def record(**changes):
    return {
        "id": news_id("niji_topics", "01_123"),
        "source": "niji_topics",
        "page_name": "01_123",
        "title": "同じタイトル",
        "published_at": "2026-09-11",
        "source_url": URL,
        "body_markdown": "old",
        "source_hash": "hash1",
        "images": [
            {"kind": "remote", "source_url": "https://example.com/image.jpg", "alt_text": "一枚"},
        ],
        **changes,
    }


class NewsStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_news_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_stable_ids_and_timestamp_on_unchanged_refresh(self):
        self.assertEqual(upsert_news_record(self.conn, record(), "2026-01-01T00:00:00+00:00"), (True, True))
        image_id = self.conn.execute("SELECT id FROM news_images").fetchone()[0]
        self.assertEqual(
            upsert_news_record(self.conn, record(source_hash="chrome-changed"), "2026-01-02T00:00:00+00:00"),
            (False, False),
        )
        self.assertEqual(self.conn.execute("SELECT id FROM news_images").fetchone()[0], image_id)
        self.assertEqual(
            self.conn.execute("SELECT updated_at FROM news_articles").fetchone()[0], "2026-01-01T00:00:00+00:00"
        )

    def test_images_only_change_and_manual_protection(self):
        upsert_news_record(self.conn, record())
        self.conn.execute("UPDATE news_articles SET title='手動', manual_fields_json='[\"title\"]'")
        self.conn.execute(
            "INSERT INTO news_images(news_id,kind,local_path,created_at) VALUES (?, 'manual', 'runtime:manual/a.png', '')",
            (record()["id"],),
        )
        self.assertEqual(upsert_news_record(self.conn, record(images=[])), (True, False))
        self.assertEqual(self.conn.execute("SELECT title FROM news_articles").fetchone()[0], "手動")
        self.assertEqual(self.conn.execute("SELECT kind FROM news_images").fetchone()[0], "manual")

    def test_local_import_stable_and_nested_headings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "[20260911]niji_topics_0001_01_123.md"
            source.write_text(
                "# 标题\n- 页面名称：01_123\n## 页面内容\n## 小节\n正文\n![图](pic/a.jpg)\n", encoding="utf-8"
            )
            parsed = parse_local_markdown(source)
            self.assertIn("## 小节", parsed["body_markdown"])
            upsert_news_record(self.conn, parsed)
            first = self.conn.execute("SELECT id FROM news_images").fetchone()[0]
            self.assertEqual(upsert_news_record(self.conn, parsed), (False, False))
            self.assertEqual(self.conn.execute("SELECT id FROM news_images").fetchone()[0], first)

    def test_existing_navigation_is_cleaned_by_schema_migration(self):
        upsert_news_record(self.conn, record())
        polluted = "- [全てのニュース](https://www.lovelive-anime.jp/nijigasaki/topics.php)\n\n- [グッズ](https://www.lovelive-anime.jp/nijigasaki/topics.php?cat=goods)\n\n正文"
        self.conn.execute("UPDATE news_articles SET body_markdown = ?", (polluted,))
        ensure_news_schema(self.conn)
        body = self.conn.execute("SELECT body_markdown FROM news_articles").fetchone()[0]
        self.assertEqual(body, "正文")

    def test_html_preserves_markdown(self):
        parsed = parse_topic_detail(HTML, URL)
        self.assertIn("**更新本文**", parsed["body_markdown"])
        self.assertIn("[リンク](https://www.lovelive-anime.jp/link)", parsed["body_markdown"])
        self.assertIn("- 項目", parsed["body_markdown"])
        with self.assertRaises(ValueError):
            parse_topic_detail("<main>Access denied</main>", URL)

    def test_detail_filters_navigation_and_category_chrome(self):
        html = '<body><div id="main"><article><ul id="contentsmenu"><li>全てのニュース</li><li>音楽商品</li><li>グッズ</li></ul></article><article><div class="newsbox"><div class="title"><p class="cat"><a href="topics.php?cat=goods">グッズ</a></p><h6>2026/09/11</h6><h5>有效标题</h5></div><div class="txt"><p>这是正文内容，长度足够通过页面有效性检查，并且包含更多文字以模拟官网真实新闻页面。</p></div></div></article></div></body>'
        parsed = parse_topic_detail(html, URL)
        self.assertEqual(parsed["title"], "有效标题")
        self.assertIn("正文内容", parsed["body_markdown"])
        self.assertNotIn("全てのニュース", parsed["body_markdown"])
        self.assertNotIn("音楽商品", parsed["body_markdown"])
        self.assertNotIn("グッズ", parsed["body_markdown"])


class NewsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.directory.name) / "test.sqlite3"),
            patch.object(main, "COLLABORATION_INDEX_PATHS", ()),
            patch.object(main, "ILLUSTRATION_MANIFEST_PATH", Path(self.directory.name) / "none"),
            patch.object(main, "news_run_lock", asyncio.Lock()),
            patch.object(main, "sync_lock", asyncio.Lock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        main.init_db()
        with main.db() as conn:
            for source in ("niji_topics", "niji_news", "as_news"):
                upsert_news_record(conn, record(id=news_id(source, "01_123"), source=source))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def login(self):
        self.client.cookies.set("nijidb_admin", main.admin_cookie_value(main.settings()["admin_password_hash"]))

    async def test_group_sources_and_filters(self):
        data = (await self.client.get("/api/news?source=official_site")).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual({item["source_label"] for item in data["items"]}, {"Official Site News"})
        self.assertEqual([item["count"] for item in data["source_options"]], [2, 1])
        self.assertEqual((await self.client.get("/api/news?source=niji_topics")).json()["total"], 1)

    async def test_refresh_auth_failure_and_manual_edit(self):
        endpoint = f"/api/admin/news/{record()['id']}"
        self.assertEqual((await self.client.post(endpoint + "/refresh")).status_code, 401)
        self.login()
        response = await self.client.patch(endpoint, json={"title": "手动标题", "body_markdown": "old"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["article"]["manual_fields"], ["title"])
        fake = httpx.Response(200, text=HTML)
        with patch.object(main, "fetch_news_page", AsyncMock(return_value=fake)):
            refreshed = await self.client.post(endpoint + "/refresh")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["article"]["title"], "手动标题")
        self.assertIn("**更新本文**", refreshed.json()["article"]["body_markdown"])
        with patch.object(main, "fetch_news_page", AsyncMock(side_effect=ValueError("官网详情未包含有效新闻内容"))):
            failed = await self.client.post(endpoint + "/refresh")
        self.assertEqual(failed.status_code, 502)
        self.assertIn("官网详情未包含有效新闻内容", failed.json()["detail"])
        self.assertEqual((await self.client.get("/api/news/" + record()["id"])).json()["article"]["title"], "手动标题")
        logs = (await self.client.get("/api/admin/settings")).json()["activity_logs"]
        self.assertTrue(any(log["category"] == "news" for log in logs))
        main.log_database_activity("collabo", "联动修改")
        self.assertTrue(
            any(
                log["category"] == "collabo"
                for log in (await self.client.get("/api/admin/settings")).json()["activity_logs"]
            )
        )

    async def test_delete_source_image_and_keep_suppressed_on_refresh(self):
        self.login()
        article_id = record()["id"]
        with main.db() as conn:
            image_id = conn.execute("SELECT id FROM news_images WHERE news_id = ?", (article_id,)).fetchone()[0]
        response = await self.client.delete(f"/api/admin/news/images/{image_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["article"]["images"], [])
        with main.db() as conn:
            upsert_news_record(conn, record())
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM news_images WHERE news_id = ?", (article_id,)).fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM news_image_suppressions WHERE news_id = ?", (article_id,)
                ).fetchone()[0],
                1,
            )

    async def test_revalidates_unchanged_listing_and_304(self):
        listing = '<ul><li><a href="/news/01_123.html">同じタイトル</a><span>2026.09.11</span></li></ul>'
        requests = []

        async def fetch(client, url, headers=None):
            requests.append((url, headers))
            if "topics.php" in url:
                return httpx.Response(200, text=listing)
            if headers and headers.get("If-None-Match") == "v1":
                return httpx.Response(304)
            return httpx.Response(200, text=HTML, headers={"etag": "v1"})

        with patch.object(main, "fetch_news_page", fetch):
            first = await main.news_sync_once()
            second = await main.news_sync_once()
        self.assertEqual(first["changed_count"], 1)
        self.assertEqual(second["changed_count"], 0)
        self.assertTrue(any(headers and headers.get("If-None-Match") == "v1" for _, headers in requests))
        self.assertTrue(all("news.php" not in url for url, _ in requests))

    async def test_refresh_busy_and_conflict(self):
        self.login()
        endpoint = f"/api/admin/news/{record()['id']}"
        async with main.news_run_lock:
            self.assertEqual((await self.client.post(endpoint + "/refresh")).status_code, 409)
            self.assertEqual((await self.client.post("/api/admin/news/sync")).status_code, 409)
        self.assertEqual(
            (await self.client.patch(endpoint, json={"title": "x", "updated_at": "outdated"})).status_code, 409
        )

    async def test_settings_wake_disabled_scheduler(self):
        self.login()
        await self.client.patch("/api/admin/settings", json={"news_auto_sync": "0"})
        stop = asyncio.Event()
        wake = asyncio.Event()
        calls = []

        async def sync():
            calls.append(True)
            stop.set()
            wake.set()

        with (
            patch.object(main, "stop_event", stop),
            patch.object(main, "news_settings_event", wake),
            patch.object(main, "wait_or_stop", AsyncMock()),
            patch.object(main, "news_sync_once", sync),
        ):
            task = asyncio.create_task(main.news_scheduler())
            try:
                await asyncio.sleep(0.01)
                self.assertEqual(calls, [])
                response = await self.client.patch("/api/admin/settings", json={"news_auto_sync": "1"})
                self.assertEqual(response.status_code, 200)
                await asyncio.wait_for(task, 1)
                self.assertEqual(calls, [True])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_network_bounds_redirects_and_retries(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(302, headers={"location": "https://127.0.0.1/secrets"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                await fetch_news_page(client, URL)
        self.assertEqual(len(calls), 1)
        attempts = []

        def flaky(request):
            attempts.append(request.url)
            return httpx.Response(503 if len(attempts) == 1 else 200, text=HTML, headers={"content-type": "text/html"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
            with patch("app.news_fetch.asyncio.sleep", AsyncMock()):
                self.assertEqual((await fetch_news_page(client, URL)).status_code, 200)
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
