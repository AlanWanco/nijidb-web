"""Offline checks for official news poller state helpers."""

from __future__ import annotations

import unittest

from scripts.local_official_news_poller import archived_image_source_set, image_source_set


class LocalNewsPollerTests(unittest.TestCase):
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
