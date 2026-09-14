"""Offline regressions: temporary SQLite only; no web crawls or runtime data writes."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.news import (
    NEWS_SUMMARY_MAX_LENGTH,
    clean_news_markdown,
    ensure_news_schema,
    image_references,
    news_id,
    normalized_tags,
    parse_local_markdown,
    parse_topic_detail,
    truncate_news_summary,
    upsert_news_record,
)
from app.news_fetch import fetch_news_page, filter_news_images

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

    def test_editor_tags_are_limited_to_known_tokens(self):
        self.assertEqual(
            normalized_tags(["goods", "anime:movie", "not-allowed"]),
            ["goods", "anime:movie"],
        )

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

    def test_legacy_html_image_markup_is_not_article_text(self):
        polluted = (
            "正文\n"
            '<img\n src="https://example.com/hero.jpg"\n srcset="https://example.com/hero-640.jpg 640w"\n width="640" />\n'
            "![图片](pic/archive.jpg)https://example.com/hero-640.jpg 640w, "
            "[https://example.com/hero-320.jpg](https://example.com/hero-320.jpg) 320w width=640\n"
            "结尾"
        )
        self.assertEqual(clean_news_markdown(polluted), "正文\n\n结尾")
        self.assertEqual(clean_news_markdown("第一段\n\n第二段"), "第一段\n\n第二段")
        self.assertEqual(clean_news_markdown("正文\nhttps://example.com/hero.jpg 640w\n结尾"), "正文\n结尾")
        self.assertEqual(
            image_references(polluted, "https://example.com/news/01.html"),
            [{"kind": "archive", "local_path": "pic/archive.jpg", "source_url": ""},
             {"kind": "remote", "local_path": "", "source_url": "https://example.com/hero.jpg"}],
        )
        self.assertEqual(
            image_references('<img src="/hero.jpg">', "https://example.com/news/01.html"),
            [{"kind": "remote", "local_path": "", "source_url": "https://example.com/hero.jpg"}],
        )

    def test_summary_is_limited_for_new_and_existing_records(self):
        long_summary = "摘要" * NEWS_SUMMARY_MAX_LENGTH
        self.assertEqual(len(truncate_news_summary(long_summary)), NEWS_SUMMARY_MAX_LENGTH)
        upsert_news_record(self.conn, record(summary=long_summary))
        self.assertEqual(
            len(self.conn.execute("SELECT summary FROM news_articles").fetchone()[0]), NEWS_SUMMARY_MAX_LENGTH
        )
        self.conn.execute(
            "UPDATE news_articles SET summary = ?, manual_fields_json = '[\"summary\"]'",
            (long_summary,),
        )
        ensure_news_schema(self.conn)
        self.assertEqual(
            len(self.conn.execute("SELECT summary FROM news_articles").fetchone()[0]), NEWS_SUMMARY_MAX_LENGTH
        )

    def test_html_preserves_markdown(self):
        parsed = parse_topic_detail(HTML, URL)
        self.assertIn("**更新本文**", parsed["body_markdown"])
        self.assertIn("[リンク](https://www.lovelive-anime.jp/link)", parsed["body_markdown"])
        self.assertIn("- 項目", parsed["body_markdown"])
        with self.assertRaises(ValueError):
            parse_topic_detail("<main>Access denied</main>", URL)

    def test_detail_filters_navigation_category_chrome_and_small_images(self):
        html = '<body><div id="main"><article><ul id="contentsmenu"><li>全てのニュース</li><li>音楽商品</li><li>グッズ</li></ul></article><article><div class="newsbox"><div class="title"><p class="cat"><a href="topics.php?cat=goods">グッズ</a></p><h6>2026/09/11</h6><h5>有效标题</h5></div><div class="txt"><p>这是正文内容，长度足够通过页面有效性检查，并且包含更多文字以模拟官网真实新闻页面。</p><img src="/tiny.png" width="80" height="80"><img src="/valid.png" width="640" height="360"></div></div></article></div></body>'
        parsed = parse_topic_detail(html, URL)
        self.assertEqual(parsed["title"], "有效标题")
        self.assertEqual(len(parsed["images"]), 1)
        self.assertEqual((parsed["images"][0]["width"], parsed["images"][0]["height"]), (640, 360))
        self.assertIn("正文内容", parsed["body_markdown"])
        self.assertNotIn("全てのニュース", parsed["body_markdown"])
        self.assertNotIn("音楽商品", parsed["body_markdown"])
        self.assertNotIn("グッズ", parsed["body_markdown"])


class NotificationFormattingTests(unittest.TestCase):
    def test_release_notification_contains_before_and_after_values(self):
        previous = {
            "title": "旧标题",
            "subtitle": "",
            "artist": "旧艺人",
            "release_date": "旧日期",
            "price": "旧价格",
            "cover_url": "https://example.com/old.jpg",
            "tracks_json": json.dumps([{"number": 1, "title": "旧曲", "credits": {}}], ensure_ascii=False),
            "spec_json": json.dumps({"仕様": "旧规格"}, ensure_ascii=False),
            "extras_json": json.dumps([{"title": "旧特典", "entries": ["旧内容"]}], ensure_ascii=False),
            "detail_html": "旧详情",
        }
        item = {
            "title": "新标题",
            "subtitle": "",
            "artist": "新艺人",
            "release_date": "新日期",
            "price": "新价格",
            "cover_url": "https://example.com/new.jpg",
            "tracks_json": json.dumps(
                [{"number": 1, "title": "旧曲", "credits": {}}, {"number": 2, "title": "新曲", "credits": {}}],
                ensure_ascii=False,
            ),
            "spec_json": json.dumps({"仕様": "新规格"}, ensure_ascii=False),
            "extras_json": json.dumps([{"title": "新特典", "entries": ["新内容"]}], ensure_ascii=False),
            "detail_html": "新详情",
            "_previous": previous,
            "_cover_changed": True,
        }
        details = main.release_change_details(item)
        self.assertIn("- 艺人：旧艺人", details)
        self.assertIn("+ 艺人：新艺人", details)
        self.assertIn("- 封面：https://example.com/old.jpg", details)
        self.assertIn("+ 封面：https://example.com/new.jpg", details)
        self.assertIn("曲目（1 首 → 2 首）", details)
        self.assertIn("+ 02 新曲", details)
        self.assertIn("- 收录/规格：旧规格", details)
        self.assertIn("+ 收录/规格：新规格", details)
        self.assertIn("- 旧特典：旧内容", details)
        self.assertIn("+ 新特典：新内容", details)

    def test_news_notification_contains_before_and_after_values(self):
        previous = {
            "title": "旧新闻标题",
            "published_at": "2026-09-10",
            "category": "旧分类",
            "source_url": "https://example.com/old",
            "tags_json": '["goods"]',
            "summary": "旧摘要",
            "body_markdown": "旧正文",
            "_images": [{"source_url": "https://example.com/old.jpg", "alt_text": "旧图"}],
        }
        item = {
            "title": "新新闻标题",
            "published_at": "2026-09-11",
            "category": "新分类",
            "source_url": "https://example.com/new",
            "tags_json": '["goods", "music"]',
            "summary": "新摘要",
            "body_markdown": "旧正文\n新增正文",
            "_images": [{"source_url": "https://example.com/new.jpg", "alt_text": "新图"}],
            "_previous": previous,
        }
        details = main.news_change_details(item)
        self.assertIn("- 标题：旧新闻标题", details)
        self.assertIn("+ 标题：新新闻标题", details)
        self.assertIn("- 标签：goods", details)
        self.assertIn("+ 标签：goods、music", details)
        self.assertIn("摘要", details)
        self.assertIn("- 旧摘要", details)
        self.assertIn("+ 新摘要", details)
        self.assertIn("正文", details)
        self.assertIn("+ 新增正文", details)
        self.assertIn("- https://example.com/old.jpg（旧图）", details)
        self.assertIn("+ https://example.com/new.jpg（新图）", details)


class NewsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_images_are_filtered_by_real_dimensions(self):
        def png(width, height):
            return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + width.to_bytes(4, "big") + height.to_bytes(4, "big")

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=png(80, 80) if "tiny" in str(request.url) else png(640, 360),
            )

        images = [
            {"kind": "remote", "source_url": "https://www.lovelive-anime.jp/module/image.php?tiny=1"},
            {"kind": "remote", "source_url": "https://www.lovelive-anime.jp/module/image.php?large=1"},
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            filtered = await filter_news_images(client, images)
        self.assertEqual(len(filtered), 1)
        self.assertEqual((filtered[0]["width"], filtered[0]["height"]), (640, 360))

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

    async def test_search_matches_article_body(self):
        article_id = news_id("niji_topics", "01_123")
        with main.db() as conn:
            conn.execute(
                "UPDATE news_articles SET body_markdown = ? WHERE id = ?",
                ("正文搜索专用关键词", article_id),
            )
        data = (await self.client.get("/api/news?q=正文搜索专用关键词")).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["id"], article_id)

    async def test_group_sources_and_filters(self):
        data = (await self.client.get("/api/news?source=official_site")).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual({item["source_label"] for item in data["items"]}, {"Official Site News"})
        self.assertEqual([item["count"] for item in data["source_options"]], [2, 1])
        self.assertEqual((await self.client.get("/api/news?source=niji_topics")).json()["total"], 1)

    async def test_slow_refresh_settings_and_queue_status(self):
        self.login()
        response = await self.client.patch(
            "/api/admin/settings",
            json={"news_slow_refresh_enabled": "1", "news_slow_refresh_delay_seconds": "2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settings"]["news_slow_refresh_enabled"], "1")
        self.assertEqual(response.json()["settings"]["news_slow_refresh_delay_seconds"], "5")
        data = (await self.client.get("/api/admin/settings")).json()
        self.assertEqual(data["news_slow_refresh"]["total"], 3)
        self.assertEqual(data["news_slow_refresh"]["pending"], 3)
        self.assertTrue(data["news_slow_refresh"]["enabled"])
        self.assertEqual(data["news_slow_refresh"]["delay_seconds"], 5)

    async def test_music_auto_sync_setting_can_be_disabled(self):
        self.login()
        response = await self.client.patch(
            "/api/admin/settings",
            json={"music_auto_sync": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settings"]["music_auto_sync"], "0")
        data = (await self.client.get("/api/admin/settings")).json()
        self.assertEqual(data["settings"]["music_auto_sync"], "0")

    async def test_database_backup_contains_program_news_and_collabo_tables(self):
        self.login()
        response = await self.client.get("/api/admin/backup")
        self.assertEqual(response.status_code, 200)
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as backup:
            backup.write(response.content)
            backup.flush()
            connection = sqlite3.connect(backup.name)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            connection.close()
        self.assertTrue({"programs", "releases", "news_articles", "collaboration_items"}.issubset(tables))

    async def test_news_upload_writes_r2_reference(self):
        self.login()
        image_dir = Path(self.directory.name) / "images" / "news"
        with (
            patch.object(main, "NEWS_RUNTIME_DIR", image_dir),
            patch.object(main, "r2_upload_is_configured", return_value=True),
            patch.object(main, "r2_is_configured", return_value=True),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "upload_image_to_r2") as upload,
        ):
            response = await self.client.post(
                f"/api/admin/news/{record()['id']}/images",
                content=b"\xff\xd8\xffnews-image",
                headers={"content-type": "image/jpeg", "x-filename": "cover.jpg"},
            )
        self.assertEqual(response.status_code, 200)
        image = response.json()["article"]["images"][-1]
        self.assertTrue(image["public_url"].startswith("https://images.example.test/images/news/"))
        upload.assert_called_once()
        with main.db() as connection:
            stored = connection.execute("SELECT public_url FROM news_images WHERE id = ?", (image["id"],)).fetchone()
        self.assertEqual(stored[0], image["public_url"])

    async def test_collabo_upload_writes_r2_reference_when_saved(self):
        self.login()
        image_dir = Path(self.directory.name) / "images" / "illustrations"
        with (
            patch.object(main, "ILLUSTRATION_RUNTIME_DIR", image_dir),
            patch.object(main, "r2_upload_is_configured", return_value=True),
            patch.object(main, "r2_is_configured", return_value=True),
            patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
            patch.object(main, "upload_image_to_r2") as upload,
        ):
            uploaded = await self.client.post(
                "/api/admin/collabo/assets",
                content=b"\xff\xd8\xffcollabo-image",
                headers={"content-type": "image/jpeg", "x-filename": "illustration.jpg"},
            )
            self.assertEqual(uploaded.status_code, 200)
            saved = await self.client.post(
                "/api/admin/collabo",
                json={
                    "title": "测试联动",
                    "date": "2026-09-11",
                    "images": uploaded.json()["images"],
                },
            )
        self.assertEqual(saved.status_code, 200)
        image = saved.json()["item"]["images"][0]
        self.assertTrue(image["public_url"].startswith("https://images.example.test/images/illustrations/"))
        upload.assert_called_once()

    async def test_collabo_delete_requires_admin_and_removes_record(self):
        self.assertEqual((await self.client.delete("/api/admin/collabo/missing")).status_code, 401)
        self.login()
        created = await self.client.post(
            "/api/admin/collabo",
            json={"title": "待删除联动", "date": "2026-09-11", "images": []},
        )
        self.assertEqual(created.status_code, 200)
        item = created.json()["item"]
        deleted = await self.client.delete(f"/api/admin/collabo/{item['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["message"], "联动已删除")
        self.assertEqual((await self.client.get(f"/api/collabo/{item['slug']}")).status_code, 404)
        with main.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM collaboration_items WHERE id = ?", (item["id"],)).fetchone())

    async def test_collabo_combination_filter_keeps_core_groups_and_uses_exact_member_sets(self):
        self.login()
        initial9 = ["ayumu", "kasumi", "shizuku", "karin", "ai", "kanata", "setsuna", "emma", "rina"]
        anime10 = initial9 + ["yu"]
        shioriko10 = initial9 + ["shioriko"]
        movie1 = ["ayumu", "shizuku", "kanata", "emma", "lanzhu", "kasumi"]
        movie2 = ["ai", "rina", "setsuna", "shioriko", "mia", "karin"]
        all_tags = initial9 + ["shioriko", "mia", "lanzhu", "yu"]
        cases = (
            ("全员", all_tags),
            ("初始9人", initial9),
            ("动画一期10人", anime10),
            ("栞子加入后10人", shioriko10),
            ("剧场版第一章", movie1),
            ("剧场版第二章", movie2),
            ("其他", initial9 + ["mia"]),
        )
        for title, tags in cases:
            response = await self.client.post(
                "/api/admin/collabo",
                json={"title": title, "date": "2026-09-11", "tags": tags, "images": []},
            )
            self.assertEqual(response.status_code, 200)
        for group, title in (
            ("all", "全员"),
            ("initial9", "初始9人"),
            ("anime10", "动画一期10人"),
            ("shioriko10", "栞子加入后10人"),
            ("movie1", "剧场版第一章"),
            ("movie2", "剧场版第二章"),
        ):
            data = (await self.client.get(f"/api/collabo?group={group}")).json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["title"], title)
        self.assertEqual((await self.client.get("/api/collabo?group=initial9&tags=mia")).json()["total"], 0)

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

    async def test_news_tag_catalog_admin_crud_and_article_sync(self):
        article_id = record()["id"]
        self.login()
        created = await self.client.post(
            "/api/admin/news/tags",
            json={"key": "test:catalog", "labels": {"zh-CN": "测试目录", "ja": "テスト", "en": "Test"}},
        )
        self.assertEqual(created.status_code, 200)
        tag = created.json()["tag"]
        updated = await self.client.patch(
            f"/api/admin/news/tags/{tag['id']}",
            json={"key": "test:renamed", "labels": {"zh-CN": "已改名"}},
        )
        self.assertEqual(updated.status_code, 200)
        changed = await self.client.patch(f"/api/admin/news/{article_id}", json={"tag_ids": [tag["id"]]})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["article"]["tags"], ["test:renamed"])
        deleted = await self.client.delete(f"/api/admin/news/tags/{tag['id']}")
        self.assertEqual(deleted.status_code, 200)
        detail = (await self.client.get(f"/api/news/{article_id}")).json()["article"]
        self.assertEqual(detail["tags"], [])
        self.assertEqual(detail["tag_ids"], [])

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

        bot_config = {
            "onebot_url": "https://bot.example.test",
            "onebot_token": "",
            "onebot_target": "123",
            "onebot_profile": "bot",
        }
        with (
            patch.object(main, "fetch_news_page", fetch),
            patch.object(main, "settings", return_value=bot_config),
            patch.object(main, "send_onebot", new_callable=AsyncMock) as send,
        ):
            first = await main.news_sync_once()
            second = await main.news_sync_once()
        self.assertEqual(first["changed_count"], 1)
        self.assertEqual(second["changed_count"], 0)
        self.assertTrue(any(headers and headers.get("If-None-Match") == "v1" for _, headers in requests))
        self.assertTrue(all("news.php" not in url for url, _ in requests))
        send.assert_awaited_once()
        message = send.await_args.args[0]
        self.assertIn("[官网新闻更新]", message)
        self.assertIn("同じタイトル", message)
        self.assertIn("正文", message)
        self.assertIn("+ **更新本文** [リンク](https://www.lovelive-anime.jp/link)", message)
        self.assertNotIn(f"+ 来源链接：{URL}", message)

    async def test_slow_refresh_uses_official_images_and_removes_archive_refs(self):
        article_id = news_id("niji_topics", "01_123")
        with main.db() as conn:
            conn.execute(
                "INSERT INTO news_images(news_id, kind, local_path, source_url, created_at) VALUES (?, 'archive', ?, '', '')",
                (article_id, "archive:pic/old.jpg"),
            )
        slow_html = HTML.replace("</main>", '<img src="/valid.jpg" width="640" height="360"></main>')
        with patch.object(main, "fetch_news_page", AsyncMock(return_value=httpx.Response(200, text=slow_html))):
            result = await main.news_slow_refresh_once()
        self.assertEqual(result["status"], "completed")
        with main.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM news_images WHERE news_id = ? AND kind = 'archive'", (article_id,)
                ).fetchone()[0],
                0,
            )
            status = main.news_slow_refresh_status(conn)
        self.assertEqual(status["completed"], 1)
        self.assertEqual(status["total"], 3)

    async def test_slow_refresh_records_risk_failures_for_later_retry(self):
        request = httpx.Request("GET", URL)
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("blocked", request=request, response=response)
        with patch.object(main, "fetch_news_page", AsyncMock(side_effect=error)):
            result = await main.news_slow_refresh_once()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["risk"])
        with main.db() as conn:
            failed = conn.execute(
                "SELECT status, risk, last_error, next_attempt_at FROM news_refresh_queue WHERE news_id = ?",
                (result["news_id"],),
            ).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["risk"], 1)
        self.assertIn("403", failed["last_error"])
        self.assertTrue(failed["next_attempt_at"])

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

    async def test_music_scheduler_skips_when_disabled_until_enabled(self):
        self.login()
        await self.client.patch("/api/admin/settings", json={"music_auto_sync": "0"})
        stop = asyncio.Event()
        wake = asyncio.Event()
        calls = []

        async def sync():
            calls.append(True)
            stop.set()

        with (
            patch.object(main, "stop_event", stop),
            patch.object(main, "music_settings_event", wake),
            patch.object(main, "sync_once", sync),
        ):
            task = asyncio.create_task(main.source_scheduler())
            try:
                await asyncio.sleep(0.01)
                self.assertEqual(calls, [])
                response = await self.client.patch("/api/admin/settings", json={"music_auto_sync": "1"})
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
