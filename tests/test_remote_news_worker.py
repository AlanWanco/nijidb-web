"""Pure offline checks for the external news image worker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.remote_news_image_worker import (
    image_dimensions,
    image_signature,
    is_r2_public_url,
    load_state,
    save_state,
)


class RemoteNewsWorkerTests(unittest.TestCase):
    def test_image_signatures_and_dimensions(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (640).to_bytes(4, "big") + (360).to_bytes(4, "big")
        self.assertEqual(image_signature(png), (".png", "image/png"))
        self.assertEqual(image_dimensions(png), (640, 360))
        self.assertIsNone(image_signature(b"<html>Access denied</html>"))

    def test_r2_public_url_is_limited_to_the_image_prefix(self):
        base = "https://pub.example.test"
        self.assertTrue(is_r2_public_url("https://pub.example.test/images/news-remote/a.jpg", base, "images"))
        self.assertFalse(is_r2_public_url("https://pub.example.test/other/a.jpg", base, "images"))
        self.assertFalse(is_r2_public_url("https://pub.example.test/images/a.jpg?x=1", base, "images"))
        self.assertFalse(is_r2_public_url("https://evil.example.test/images/a.jpg", base, "images"))

    def test_state_is_written_atomically_with_default_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "worker.json"
            state = load_state(path)
            state["article_ids"] = ["one"]
            save_state(path, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_state(path)
        self.assertEqual(loaded["article_ids"], ["one"])


if __name__ == "__main__":
    unittest.main()
