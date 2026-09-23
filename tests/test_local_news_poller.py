"""Offline checks for official news poller helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.local_official_news_poller import (
    PollerConfigError,
    archived_image_source_set,
    build_config,
    image_source_set,
    parse_args,
)


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


if __name__ == "__main__":
    unittest.main()
