"""Regression tests for the server-side Vue history fallback."""

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


class FrontendRoutingTests(unittest.TestCase):
    def test_only_known_vue_routes_are_allowed(self):
        for path in (
            "",
            "music",
            "news",
            "news/0123456789abcdef",
            "release/cd01_6289",
            "programs",
            "programs/202609",
            "programs/archive",
            "programs/archive/program-test",
            "illustrations",
            "collabo",
            "collabo/20260917-abcdef",
            "admin/login",
            "admin",
            "admin/programs",
            "admin/collabo",
            "admin/collabo/new",
            "music/",
        ):
            with self.subTest(path=path):
                self.assertTrue(main.is_frontend_route(path))

        for path in (
            "phpinfo.php",
            "wp-admin",
            "wp-login.php",
            "api",
            "api/unknown",
            "assets/missing.js",
            "news/not-an-extension.php",
            "programs/not-a-month",
            "collabo/20260917-not-a-slug",
        ):
            with self.subTest(path=path):
                self.assertFalse(main.is_frontend_route(path))

    def test_fallback_returns_404_for_unknown_paths(self):
        async def check() -> None:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for path in ("/phpinfo.php", "/wp-admin", "/api/unknown"):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 404, path)

        asyncio.run(check())

    def test_fallback_serves_index_for_known_route(self):
        async def check() -> None:
            with tempfile.TemporaryDirectory() as directory:
                index = Path(directory) / "index.html"
                index.write_text("<html>test</html>", encoding="utf-8")
                with patch.object(main, "FRONTEND_DIST", Path(directory)):
                    transport = httpx.ASGITransport(app=main.app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                        response = await client.get("/programs/202609")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "<html>test</html>")

        asyncio.run(check())
