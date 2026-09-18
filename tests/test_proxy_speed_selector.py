"""Tests for the shared proxy throughput selector."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.proxy_speed_selector import (
    ProxySpeedSelector,
    SpeedResult,
    choose_selected,
    speed_host_is_safe,
    speed_host_resolves_global,
    valid_http_url,
)


class ProxySpeedSelectorTests(unittest.TestCase):
    def test_choose_selected_prefers_fastest_node(self):
        results = [
            SpeedResult("slow", 7901, 100, 1),
            SpeedResult("fast", 7902, 500, 1),
        ]
        self.assertEqual(choose_selected(results, "slow", 0.15).name, "fast")

    def test_choose_selected_uses_hysteresis_for_small_difference(self):
        results = [
            SpeedResult("current", 7901, 1000, 1),
            SpeedResult("slightly-faster", 7902, 1100, 1),
        ]
        self.assertEqual(choose_selected(results, "current", 0.15).name, "current")

    def test_candidate_names_prioritize_current_and_previous_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nodes = root / "nodes.json"
            state = root / "speed.json"
            nodes.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"node": "healthy-late", "status": 200},
                            {"node": "healthy-fast", "status": 200},
                            {"node": "current-unhealthy", "status": 403},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.write_text(json.dumps({"selected": "healthy-late"}), encoding="utf-8")
            selector = ProxySpeedSelector(
                config_path=root / "config.yaml",
                state_path=state,
                nodes_path=nodes,
                controller="http://mihomo:9090",
                group="PROXY",
                proxy_host="mihomo",
                speed_url="https://example.invalid/test",
                candidate_count=3,
                bytes_to_read=65536,
                timeout_seconds=5,
                min_gain=0.15,
                start_delay_seconds=0,
            )
            selected = selector.candidate_names(
                {"now": "current-unhealthy", "all": ["healthy-fast", "healthy-late", "current-unhealthy"]},
                {
                    "healthy-fast": {"history": [{"delay": 20}]},
                    "healthy-late": {"history": [{"delay": 30}]},
                    "current-unhealthy": {"history": [{"delay": 1}]},
                },
                {"healthy-fast": 7901, "healthy-late": 7902, "current-unhealthy": 7903},
            )
            self.assertEqual(selected, ["current-unhealthy", "healthy-late", "healthy-fast"])

    def test_speed_url_rejects_legacy_private_ipv4_forms(self):
        for host in ("127.1", "2130706433", "0x7f000001", "0"):
            with self.subTest(host=host):
                self.assertFalse(speed_host_is_safe(host))

    def test_speed_url_dns_check_rejects_private_resolution(self):
        records = [(2, 1, 6, "", ("192.168.1.20", 0))]
        with patch("scripts.proxy_speed_selector.socket.getaddrinfo", return_value=records):
            self.assertFalse(speed_host_resolves_global("speed.example.com"))

    def test_speed_url_validator_rejects_empty_query_or_fragment(self):
        for value in ("https://speed.example.com/test?", "https://speed.example.com/test#fragment"):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    valid_http_url(value, "测速地址")

    def test_choose_selected_rejects_all_failed_results(self):
        with self.assertRaisesRegex(RuntimeError, "候选节点均未完成测速"):
            choose_selected([SpeedResult("failed", 7901, error="timeout")], "failed", 0.15)


if __name__ == "__main__":
    unittest.main()
