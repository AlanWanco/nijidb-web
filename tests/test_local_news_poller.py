"""Offline checks for official news poller helpers."""

from __future__ import annotations

import copy
import os
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx

from scripts.local_official_news_poller import (
    OfficialNewsPoller,
    PollerConfig,
    PollerConfigError,
    archived_image_source_set,
    build_config,
    content_signature,
    default_state,
    direct_api_client,
    image_source_set,
    official_news_client,
    parse_args,
)


def poller_config() -> PollerConfig:
    with patch.dict(os.environ, {"NEWS_POLL_PROXY": "http://192.168.10.75:7890"}, clear=True):
        config = build_config(parse_args(["--dry-run"]))
    return replace(config, dry_run=False, image_delay=0, r2_public_base_url="https://images.example.test")


def bare_poller(record: dict, pushed_sources: list[str]) -> OfficialNewsPoller:
    poller = OfficialNewsPoller.__new__(OfficialNewsPoller)
    poller.config = poller_config()
    poller.state = default_state()
    signature = content_signature(record)
    poller.state["articles"][record["page_name"]] = {
        "content_hash": signature,
        "pushed_hash": signature,
        "pushed_image_sources": pushed_sources,
        "pushed_at": "2026-10-15T00:00:00+00:00",
    }
    poller.production = {
        record["page_name"]: {
            "id": record["id"],
            "image_count": len(record["images"]),
            "r2_image_count": len(record["images"]),
            "cover_url": "https://images.example.test/images/news-remote/a.jpg",
        }
    }
    poller.s3 = None
    return poller


def sample_article() -> dict:
    return {
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


class LocalNewsPollerTests(unittest.TestCase):
    def test_fixed_proxy_configuration_needs_no_per_node_state(self):
        proxy_url = "http://192.168.10.75:7890"
        with patch.dict(os.environ, {"NEWS_POLL_PROXY": proxy_url}, clear=True):
            config = build_config(parse_args(["--dry-run"]))
        self.assertEqual(config.proxy, proxy_url)
        self.assertFalse(hasattr(config, "nodes_file"))
        self.assertFalse(hasattr(config, "config_file"))

    def test_build_config_rejects_proxy_credentials(self):
        with (
            patch.dict(os.environ, {"NEWS_POLL_PROXY": "http://user:secret@proxy.example:7890"}, clear=True),
            self.assertRaises(PollerConfigError),
        ):
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


class LocalNewsPollerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_clients_pin_ipv4_for_the_api_and_keep_the_shared_proxy_for_the_site(self):
        config = poller_config()
        async with direct_api_client(config) as api, official_news_client(config) as official:
            self.assertEqual(list(api._mounts), [])
            self.assertEqual(api._transport._pool._local_address, "0.0.0.0")
            self.assertEqual(api._transport._pool._retries, 2)
            self.assertEqual(len(official._mounts), 1)
        async with official_news_client(replace(config, proxy="")) as direct_official:
            self.assertEqual(len(direct_official._mounts), 0)

    async def test_unchanged_article_with_stored_pushed_sources_is_skipped(self):
        record = sample_article()
        poller = bare_poller(record, [record["images"][0]["source_url"]])
        response = httpx.Response(200, text="<html>stub</html>")
        with (
            patch("scripts.local_official_news_poller.fetch_news_page", new=AsyncMock(return_value=response)),
            patch("scripts.local_official_news_poller.parse_topic_detail", return_value=copy.deepcopy(record)),
            patch.object(poller, "fetch_image_bytes", new_callable=AsyncMock) as fetch_image,
            patch.object(poller, "submit", new_callable=AsyncMock) as submit,
        ):
            handled = await poller.process_entry(None, None, record)
        self.assertFalse(handled)
        self.assertEqual(poller.state["stats"]["skipped_unchanged"], 1)
        self.assertEqual(poller.state["stats"]["pushed"], 0)
        self.assertEqual(poller.state["articles"][record["page_name"]]["pushed_at"], "2026-10-15T00:00:00+00:00")
        fetch_image.assert_not_awaited()
        submit.assert_not_awaited()

    async def test_article_with_an_unsent_image_still_submits_even_when_the_site_counts_match(self):
        record = sample_article()
        record["images"].append({"kind": "remote", "source_url": "https://www.lovelive-anime.jp/b.jpg"})
        poller = bare_poller(record, [record["images"][0]["source_url"]])
        response = httpx.Response(200, text="<html>stub</html>")
        archived = {
            "public_url": "https://images.example.test/images/news-remote/image.jpg",
            "width": 640,
            "height": 480,
            "bytes": 100,
            "sha256": "a" * 64,
        }
        with (
            patch("scripts.local_official_news_poller.fetch_news_page", new=AsyncMock(return_value=response)),
            patch("scripts.local_official_news_poller.parse_topic_detail", return_value=copy.deepcopy(record)),
            patch(
                "scripts.local_official_news_poller.filter_news_images", new=AsyncMock(return_value=record["images"])
            ),
            patch.object(poller, "fetch_image_bytes", new=AsyncMock(return_value=b"stub")),
            patch.object(poller, "archive_image", return_value=archived),
            patch.object(poller, "submit", new_callable=AsyncMock) as submit,
        ):
            handled = await poller.process_entry(None, None, record)
        self.assertTrue(handled)
        self.assertEqual(poller.state["stats"]["pushed"], 1)
        self.assertEqual(poller.state["stats"]["skipped_unchanged"], 0)
        self.assertEqual(len(submit.await_args.args[1]["images"]), 2)
        self.assertEqual(
            poller.state["articles"][record["page_name"]]["pushed_image_sources"],
            sorted(image["source_url"] for image in record["images"]),
        )


if __name__ == "__main__":
    unittest.main()
