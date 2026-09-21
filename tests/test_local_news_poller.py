"""Offline checks for official news poller state helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.local_official_news_poller import (
    OfficialNewsPoller,
    archived_image_source_set,
    image_source_set,
    proxy_region,
    region_aware_candidate_pool,
)


class LocalNewsPollerTests(unittest.TestCase):
    def test_proxy_region_uses_primary_location_not_transit_label(self):
        self.assertEqual(proxy_region("Z 土耳其01 (香港中转)"), "turkey")
        self.assertEqual(proxy_region("Z 新加坡A02 (台湾中转)"), "singapore")
        self.assertEqual(proxy_region("California (美国) A01 (IPv6)"), "united-states")
        self.assertEqual(proxy_region("A Series - HK 07"), "hong-kong")

    def test_region_aware_candidate_pool_prefers_a_different_region(self):
        candidates = [
            {"node": "Japan A01", "port": 7901},
            {"node": "Japan A02", "port": 7902},
            {"node": "Hong Kong C01", "port": 7903},
        ]
        pool = region_aware_candidate_pool(candidates, "Japan A01", "japan")
        self.assertEqual([item["node"] for item in pool], ["Hong Kong C01"])

    def test_region_aware_candidate_pool_falls_back_when_no_other_region_is_healthy(self):
        candidates = [
            {"node": "Japan A01", "port": 7901},
            {"node": "Japan A02", "port": 7902},
        ]
        pool = region_aware_candidate_pool(candidates, "Japan A01", "japan")
        self.assertEqual([item["node"] for item in pool], ["Japan A02"])

    def test_choose_proxy_persists_selected_region(self):
        poller = object.__new__(OfficialNewsPoller)
        poller.config = SimpleNamespace(proxy="http://mihomo:7890", proxy_host="mihomo")
        poller.state = {"last_node": "Japan A01", "last_region": "japan"}
        candidates = [
            {"node": "Japan A02", "port": 7901, "status": 200},
            {"node": "Hong Kong C01", "port": 7902, "status": 200},
        ]
        with (
            patch.object(poller, "read_nodes", return_value=candidates),
            patch.object(poller, "node_has_recent_hint", return_value=False),
            patch("scripts.local_official_news_poller.random.choice", side_effect=lambda items: items[0]),
        ):
            proxy, name = poller.choose_proxy()
        self.assertEqual(proxy, "http://mihomo:7902")
        self.assertEqual(name, "Hong Kong C01")
        self.assertEqual(poller.state["last_region"], "hong-kong")

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


if __name__ == "__main__":
    unittest.main()
