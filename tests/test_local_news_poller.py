"""Offline checks for official news poller helpers."""

from __future__ import annotations

import asyncio
import os
import types
import unittest
from unittest.mock import patch

from scripts.local_official_news_poller import (
    OfficialNewsPoller,
    PollerConfigError,
    archived_image_source_set,
    build_config,
    content_signature,
    direct_api_client,
    image_source_set,
    official_news_client,
    parse_args,
)


def poller_config(**overrides) -> types.SimpleNamespace:
    values = {
        "proxy": "http://192.168.10.75:7890",
        "timeout": 45.0,
        "refresh_existing": False,
        "dry_run": False,
        "r2_public_base_url": "https://images.example.test",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def bare_poller(*, config: dict | None = None, production: dict | None = None, articles: dict | None = None):
    poller = OfficialNewsPoller.__new__(OfficialNewsPoller)
    poller.config = poller_config(**(config or {}))
    poller.state = {
        "version": 1,
        "articles": dict(articles or {}),
        "stats": {
            "passes": 0,
            "pushed": 0,
            "seeded": 0,
            "skipped_unchanged": 0,
            "skipped_existing": 0,
            "errors": 0,
        },
        "backoff_until": 0.0,
        "last_pass_at": 0.0,
    }
    poller.production = dict(production or {})
    poller.s3 = None
    return poller


class StubNewsResponse:
    status_code = 200
    text = "<html><body>stub</body></html>"
    headers: dict[str, str] = {}


async def stub_fetch_news_page(client, url, headers=None):
    return StubNewsResponse()


def unchanged_entry() -> tuple[dict, dict, dict]:
    """一篇文章、它的官网条目、以及站点侧已归档记录的公共字段。"""
    record = {
        "id": "article-id",
        "source": "niji_topics",
        "page_name": "01_123",
        "title": "测试新闻",
        "published_at": "2026-10-16",
        "category": "live",
        "tags": [],
        "summary": "摘要",
        "body_markdown": "正文",
        "source_url": "https://www.lovelive-anime.jp/nijigasaki/news/01_123.html",
        "source_hash": "source-hash",
        "images": [{"kind": "remote", "source_url": "https://www.lovelive-anime.jp/a.jpg"}],
    }
    entry = {
        "page_name": record["page_name"],
        "source_url": record["source_url"],
        "title": record["title"],
        "published_at": record["published_at"],
        "category": record["category"],
    }
    production = {
        record["page_name"]: {
            "id": record["id"],
            "image_count": 1,
            "r2_image_count": 1,
            "cover_url": "https://images.example.test/images/news-remote/a.jpg",
        }
    }
    return record, entry, production


class LocalNewsPollerTests(unittest.TestCase):
    def test_fixed_proxy_configuration_needs_no_per_node_state(self):
        proxy_url = "http://192.168.10.75:7890"
        with patch.dict(os.environ, {"NEWS_POLL_PROXY": proxy_url}, clear=True):
            config = build_config(parse_args(["--dry-run"]))
        self.assertEqual(config.proxy, proxy_url)
        self.assertFalse(hasattr(config, "nodes_file"))
        self.assertFalse(hasattr(config, "config_file"))

    def test_build_config_rejects_proxy_credentials(self):
        with patch.dict(os.environ, {"NEWS_POLL_PROXY": "http://user:secret@proxy.example:7890"}, clear=True):
            with self.assertRaises(PollerConfigError):
                build_config(parse_args(["--dry-run"]))

    def test_image_source_sets_distinguish_archived_and_pending_items(self):
        images = [
            {"source_url": "https://www.lovelive-anime.jp/a.jpg", "public_url": "https://r2/a.jpg"},
            {"source_url": "https://www.lovelive-anime.jp/b.jpg", "public_url": ""},
            {"source_url": "https://www.lovelive-anime.jp/c.jpg"},
        ]
        self.assertEqual(
            image_source_set(images),
            {
                "https://www.lovelive-anime.jp/a.jpg",
                "https://www.lovelive-anime.jp/b.jpg",
                "https://www.lovelive-anime.jp/c.jpg",
            },
        )
        self.assertEqual(archived_image_source_set(images), {"https://www.lovelive-anime.jp/a.jpg"})

    def test_image_source_set_also_reads_stored_source_strings(self):
        # pushed_image_sources is persisted as a plain list of URLs, so the same
        # helper must read both shapes; otherwise every pass resubmits unchanged news.
        stored = [
            "https://www.lovelive-anime.jp/a.jpg",
            " https://www.lovelive-anime.jp/b.jpg ",
            "",
            None,
        ]
        self.assertEqual(
            image_source_set(stored),
            {"https://www.lovelive-anime.jp/a.jpg", "https://www.lovelive-anime.jp/b.jpg"},
        )
        self.assertEqual(image_source_set("https://www.lovelive-anime.jp/a.jpg"), set())

    def test_clients_pin_ipv4_for_the_api_and_keep_the_shared_proxy_for_the_site(self):
        api_client = direct_api_client(poller_config())
        self.addCleanup(lambda: None)
        self.assertEqual(list(api_client._mounts), [])
        self.assertEqual(api_client._transport._pool._local_address, "0.0.0.0")
        news_client = official_news_client(poller_config())
        self.assertEqual(len(news_client._mounts), 1)
        news_client_without_proxy = official_news_client(poller_config(proxy=""))
        self.assertEqual(len(news_client_without_proxy._mounts), 0)

    def test_unchanged_article_with_stored_pushed_sources_is_skipped(self):
        record, entry, production = unchanged_entry()
        signature = content_signature(record)
        poller = bare_poller(
            production=production,
            articles={
                record["page_name"]: {
                    "content_hash": signature,
                    "pushed_hash": signature,
                    "pushed_image_sources": ["https://www.lovelive-anime.jp/a.jpg"],
                }
            },
        )
        with patch("scripts.local_official_news_poller.fetch_news_page", stub_fetch_news_page), patch(
            "scripts.local_official_news_poller.parse_topic_detail", lambda *args, **kwargs: dict(record)
        ):
            handled = asyncio.run(poller.process_entry(None, None, entry))
        self.assertFalse(handled)
        self.assertEqual(poller.state["stats"]["skipped_unchanged"], 1)
        self.assertEqual(poller.state["stats"]["pushed"], 0)
        # 未重新提交就不会刷新提交时间戳，官网也不会被再次抓图。
        self.assertNotIn("pushed_at", poller.state["articles"][record["page_name"]])

    def test_article_with_an_unsent_image_is_not_skipped(self):
        record, entry, production = unchanged_entry()
        record["images"] = [
            {"kind": "remote", "source_url": "https://www.lovelive-anime.jp/a.jpg"},
            {"kind": "remote", "source_url": "https://www.lovelive-anime.jp/b.jpg"},
        ]
        production[record["page_name"]]["image_count"] = 2
        signature = content_signature(record)
        poller = bare_poller(
            production=production,
            articles={
                record["page_name"]: {
                    "content_hash": signature,
                    "pushed_hash": signature,
                    "pushed_image_sources": ["https://www.lovelive-anime.jp/a.jpg"],
                }
            },
        )
        # 未归档的第二张图必须继续进入补归档分支，而不是被当成“内容未变”跳过。
        self.assertEqual(
            image_source_set([{"source_url": "https://www.lovelive-anime.jp/b.jpg"}]),
            {"https://www.lovelive-anime.jp/b.jpg"},
        )
        self.assertNotEqual(
            image_source_set(record["images"]),
            image_source_set(poller.state["articles"][record["page_name"]]["pushed_image_sources"]),
        )


if __name__ == "__main__":
    unittest.main()
