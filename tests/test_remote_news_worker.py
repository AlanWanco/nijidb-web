"""Pure offline checks for the external news image worker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

import boto3

from scripts.news_network import pin_r2_client_to_ipv4
from scripts.remote_news_image_worker import (
    DirectAPIHTTPHandler,
    DirectAPIHTTPSHandler,
    NewsImageWorker,
    image_dimensions,
    image_signature,
    iri_to_uri,
    is_r2_public_url,
    load_state,
    save_state,
)


class _FakeResponse:
    status = 200

    def __init__(self):
        self.headers = {}
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        if self._read:
            return b""
        self._read = True
        return b"{}"


class _RecordingOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _FakeResponse()


class RemoteNewsWorkerTests(unittest.TestCase):
    def test_image_signatures_and_dimensions(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (360).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
        )
        self.assertEqual(image_signature(png), (".png", "image/png"))
        self.assertEqual(image_dimensions(png), (640, 360))
        self.assertIsNone(image_signature(b"<html>Access denied</html>"))

    def test_r2_public_url_is_limited_to_the_image_prefix(self):
        base = "https://pub.example.test"
        self.assertTrue(is_r2_public_url("https://pub.example.test/images/news-remote/a.jpg", base, "images"))
        self.assertFalse(is_r2_public_url("https://pub.example.test/other/a.jpg", base, "images"))
        self.assertFalse(is_r2_public_url("https://pub.example.test/images/a.jpg?x=1", base, "images"))
        self.assertFalse(is_r2_public_url("https://pub.example.test/images/news-remote//a.jpg", base, "images"))
        self.assertFalse(is_r2_public_url("https://evil.example.test/images/a.jpg", base, "images"))

    def test_unicode_url_is_percent_encoded_before_urllib_request(self):
        worker = object.__new__(NewsImageWorker)
        worker.config = SimpleNamespace(request_timeout_seconds=5)
        worker.opener = _RecordingOpener()
        source = "https://img.sunrise-inc.co.jp/assets/虹/かすみ%20.jpg?name=優木&size=full"

        self.assertEqual(
            worker.request_bytes(
                source,
                {"Referer": "https://www.lovelive-anime.jp/nijigasaki/news.php?title=虹"},
                100,
                {"img.sunrise-inc.co.jp"},
                True,
            ),
            b"{}",
        )
        request, _timeout = worker.opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://img.sunrise-inc.co.jp/assets/%E8%99%B9/%E3%81%8B%E3%81%99%E3%81%BF%20.jpg"
            "?name=%E5%84%AA%E6%9C%A8&size=full",
        )
        self.assertEqual(
            iri_to_uri("https://img.sunrise-inc.co.jp/a%20b.jpg"),
            "https://img.sunrise-inc.co.jp/a%20b.jpg",
        )
        self.assertTrue(request.full_url.isascii())
        self.assertEqual(
            request.get_header("Referer"),
            "https://www.lovelive-anime.jp/nijigasaki/news.php?title=%E8%99%B9",
        )

    def test_api_submission_uses_direct_opener_not_image_proxy(self):
        worker = object.__new__(NewsImageWorker)
        worker.config = SimpleNamespace(
            base_url="https://site.example.test",
            api_key="test-api-key",
            request_timeout_seconds=5,
        )
        worker.api_opener = _RecordingOpener()
        worker.opener = _RecordingOpener()
        self.assertEqual(worker.post_json("/api/ingest/news", {"article": {}}), {})
        self.assertEqual(len(worker.api_opener.requests), 1)
        self.assertEqual(len(worker.opener.requests), 0)
        request, timeout = worker.api_opener.requests[0]
        self.assertEqual(request.get_header("X-nijidb-api-key"), "test-api-key")
        self.assertEqual(timeout, 5)

    def test_direct_api_connections_pin_ipv4_and_keep_https_verification(self):
        for handler_type, scheme in ((DirectAPIHTTPHandler, "http"), (DirectAPIHTTPSHandler, "https")):
            with self.subTest(scheme=scheme):
                handler = handler_type()
                request = Request(f"{scheme}://site.example.test/api/news")
                with patch.object(handler, "do_open", return_value="response") as open_connection:
                    response = getattr(handler, f"{scheme}_open")(request)
                self.assertEqual(response, "response")
                factory = open_connection.call_args.args[0]
                connection = factory("site.example.test", **open_connection.call_args.kwargs)
                self.assertEqual(connection.source_address, ("0.0.0.0", 0))
                if scheme == "https":
                    self.assertTrue(connection._context.check_hostname)
                connection.close()

    def test_r2_ipv4_binding_is_limited_to_one_client(self):
        first = boto3.client(
            "s3", endpoint_url="https://r2.example.test", aws_access_key_id="test", aws_secret_access_key="test"
        )
        second = boto3.client(
            "s3", endpoint_url="https://r2.example.test", aws_access_key_id="test", aws_secret_access_key="test"
        )
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        pin_r2_client_to_ipv4(first)
        first_pool = first._endpoint.http_session._manager.connection_from_url("https://r2.example.test")
        second_pool = second._endpoint.http_session._manager.connection_from_url("https://r2.example.test")
        self.assertEqual(first_pool.conn_kw["source_address"], ("0.0.0.0", 0))
        self.assertNotIn("source_address", second_pool.conn_kw)

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
