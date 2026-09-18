"""Offline checks for proxy node health state handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.proxy_node_health import NodeHealthChecker


class ProxyNodeHealthTests(unittest.TestCase):
    def test_bad_cursor_advances_and_is_returned_to_state_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "nodes.json"
            state_path.write_text(
                json.dumps(
                    {
                        "bad_cursor": 0,
                        "nodes": [
                            {"node": "bad-1", "port": 7901, "status": 403},
                            {"node": "bad-2", "port": 7902, "status": 403},
                            {"node": "bad-3", "port": 7903, "status": 403},
                            {"node": "bad-4", "port": 7904, "status": 403},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            checker = NodeHealthChecker(
                config_path=Path(temporary) / "config.yaml",
                state_path=state_path,
                hints_path=Path(temporary) / "hints.json",
                subscription_path=Path(temporary) / "subscription.json",
                proxy_host="mihomo",
                official_url="https://example.invalid/",
                interval_seconds=1800,
                timeout=5,
                bad_batch=2,
                probe_delay=0,
                concurrency=1,
            )
            listeners = [
                {"name": f"node-{index}", "node": f"bad-{index}", "port": 7900 + index}
                for index in range(1, 5)
            ]
            targets, updated_state = checker.probe_targets(listeners)
            self.assertEqual([item["node"] for item in targets], ["bad-1", "bad-2"])
            self.assertEqual(updated_state["bad_cursor"], 2)
            state_path.write_text(json.dumps(updated_state), encoding="utf-8")

            targets, updated_state = checker.probe_targets(listeners)
            self.assertEqual([item["node"] for item in targets], ["bad-3", "bad-4"])
            self.assertEqual(updated_state["bad_cursor"], 0)


if __name__ == "__main__":
    unittest.main()
