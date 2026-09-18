"""Tests for the generated mihomo proxy configuration."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import yaml

from scripts.mihomo_config import DEFAULT_GROUP_TYPE, render, validate_controller_listen


class MihomoConfigTests(unittest.TestCase):
    def setUp(self):
        self.secret_patch = patch.dict(os.environ, {"MIHOMO_CONTROLLER_SECRET": "test-controller-secret-0123456789"}, clear=False)
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)

    def test_default_group_uses_fallback_for_speed_selector(self):
        config = yaml.safe_load(
            render(
                ["demo"],
                {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
            )
        )
        group = config["proxy-groups"][0]
        self.assertEqual(DEFAULT_GROUP_TYPE, "fallback")
        self.assertEqual(group["type"], "fallback")
        self.assertEqual(group["url"], "https://www.gstatic.com/generate_204")
        self.assertEqual(group["interval"], 120)

    def test_explicit_url_test_remains_available_for_specialized_deployments(self):
        config = yaml.safe_load(
            render(
                ["demo"],
                {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
                group_type="url-test",
            )
        )
        self.assertEqual(config["proxy-groups"][0]["type"], "url-test")

    def test_missing_controller_secret_rejects_config_generation(self):
        with patch.dict(os.environ, {"MIHOMO_CONTROLLER_SECRET": ""}, clear=False):
            with self.assertRaisesRegex(Exception, "MIHOMO_CONTROLLER_SECRET"):
                render(
                    ["demo"],
                    {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
                )

    def test_controller_listen_rejects_urls_and_invalid_ports(self):
        with self.assertRaisesRegex(Exception, "external-controller"):
            validate_controller_listen("http://mihomo:9090")
        with self.assertRaisesRegex(Exception, "端口"):
            validate_controller_listen("mihomo:0")

    def test_controller_secret_is_written_only_when_configured(self):
        config = yaml.safe_load(
            render(
                ["demo"],
                {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
            )
        )
        self.assertEqual(config["secret"], "test-controller-secret-0123456789")


if __name__ == "__main__":
    unittest.main()
