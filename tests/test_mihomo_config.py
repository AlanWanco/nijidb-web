"""Tests for the generated mihomo proxy configuration."""

from __future__ import annotations

import unittest

import yaml

from scripts.mihomo_config import DEFAULT_GROUP_TYPE, render


class MihomoConfigTests(unittest.TestCase):
    def test_default_group_auto_selects_a_healthy_low_latency_node(self):
        config = yaml.safe_load(
            render(
                ["demo"],
                {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
            )
        )
        group = config["proxy-groups"][0]
        self.assertEqual(DEFAULT_GROUP_TYPE, "url-test")
        self.assertEqual(group["type"], "url-test")
        self.assertEqual(group["url"], "https://www.gstatic.com/generate_204")
        self.assertEqual(group["interval"], 120)

    def test_explicit_fallback_remains_available_for_specialized_deployments(self):
        config = yaml.safe_load(
            render(
                ["demo"],
                {"demo": {"name": "demo", "type": "http", "server": "example.invalid", "port": 443}},
                group_type="fallback",
            )
        )
        self.assertEqual(config["proxy-groups"][0]["type"], "fallback")


if __name__ == "__main__":
    unittest.main()
