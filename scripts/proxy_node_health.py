#!/usr/bin/env python3
"""逐节点探测官网可达性，产出轮询可用的节点列表。

渲染后的 mihomo 配置为每个节点开了一个入口端口；本服务串行地通过这些端口请求官网，
把结果写到 ``nodes.json``，轮询每趟从「最近一次探测为 200」的节点里随机挑一个。

因此：**只有真正能拿到 200 的节点才会被轮询使用**，被墙、失效或返回 403 的节点自动排除，
不需要人工维护节点名单。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mihomo_config import ConfigError, read_listeners  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from logfmt import format_message  # noqa: E402
from app.news_fetch import NEWS_HEADERS  # noqa: E402

OK_STATUSES = {200, 206}


def log(message: str) -> None:
    print(format_message(message), flush=True)


def env_number(name: str, default: float, minimum: float = 1) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log(f"环境变量 {name} 不是数字，改用默认值 {default:g}")
        return default
    return value if value >= minimum else default


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class NodeHealthChecker:
    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        hints_path: Path,
        subscription_path: Path,
        proxy_host: str,
        official_url: str,
        interval_seconds: float,
        timeout: float,
        bad_batch: int,
        probe_delay: float,
        concurrency: int,
    ):
        self.config_path = config_path
        self.state_path = state_path
        self.hints_path = hints_path
        self.subscription_path = subscription_path
        self.proxy_host = proxy_host
        self.official_url = official_url
        self.interval_seconds = interval_seconds
        self.timeout = timeout
        self.bad_batch = bad_batch
        self.probe_delay = probe_delay
        self.concurrency = concurrency
        self.stop_requested = False
        self.last_check_started_at = 0.0

    def subscription_updated_after(self, moment: float) -> bool:
        """订阅刚更新过（节点与端口可能变化）时，立即重新探测一轮。"""
        raw = str(read_json(self.subscription_path).get("updated_at") or "")
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return False
        return parsed > moment

    def recent_hints(self) -> dict[str, float]:
        hints = read_json(self.hints_path).get("nodes")
        if not isinstance(hints, dict):
            return {}
        result: dict[str, float] = {}
        for name, value in hints.items():
            try:
                result[str(name)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    async def probe(self, port: int) -> tuple[int, str]:
        proxy = f"http://{self.proxy_host}:{port}"
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=self.timeout, follow_redirects=False) as client:
                response = await client.get(self.official_url, headers=NEWS_HEADERS)
        except (httpx.HTTPError, OSError) as exc:
            return 0, type(exc).__name__
        if response.status_code in OK_STATUSES:
            return response.status_code, ""
        return response.status_code, f"HTTP {response.status_code}"

    def probe_targets(self, listeners: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """决定本轮要探测哪些节点。

        * 首次运行（没有历史结果）—— 全量探测，先摸清哪些能用；
        * 之后每轮 —— 所有上次可用的节点 + 一小批轮换的不可用节点，
          既能快速发现可用节点失效，也不会对官网造成突发请求。
        未被探测的节点沿用上一次的结果。
        """
        previous_raw = read_json(self.state_path)
        previous = {
            str(item.get("port")): item
            for item in (previous_raw.get("nodes") or [])
            if isinstance(item, dict) and item.get("port") is not None
        }
        mapping = {str(item["port"]): str(item["node"]) for item in listeners}
        previous_mapping = {port: str(item.get("node") or "") for port, item in previous.items()}
        if not previous or mapping != previous_mapping:
            # 首次运行，或订阅更新导致「端口 ↔ 节点」映射变化：全量重探
            return listeners, previous_raw
        good = [item for item in listeners if previous.get(str(item["port"]), {}).get("status") in OK_STATUSES]
        bad = [item for item in listeners if previous.get(str(item["port"]), {}).get("status") not in OK_STATUSES]
        picked: list[dict[str, Any]] = []
        if bad and self.bad_batch > 0:
            cursor = int(previous_raw.get("bad_cursor") or 0)
            count = min(self.bad_batch, len(bad))
            picked = [bad[(cursor + index) % len(bad)] for index in range(count)]
            previous_raw["bad_cursor"] = (cursor + count) % len(bad)
        return good + picked, previous_raw

    async def check_once(self) -> dict[str, Any]:
        try:
            listeners = read_listeners(self.config_path)
        except ConfigError as exc:
            log(f"读取配置失败：{exc}")
            return {}
        previous_raw = read_json(self.state_path)
        previous = {
            str(item.get("port")): item
            for item in (previous_raw.get("nodes") or [])
            if isinstance(item, dict) and item.get("port") is not None
        }
        mapping = {str(item["port"]): str(item["node"]) for item in listeners}
        previous_mapping = {port: str(item.get("node") or "") for port, item in previous.items()}
        mapping_changed = bool(previous) and mapping != previous_mapping
        targets, _ = self.probe_targets(listeners)
        initial = not previous
        if initial or mapping_changed:
            reason = "首次运行" if initial else "节点映射已变化"
            log(
                f"{reason}：全量探测 {len(targets)} 个节点"
                f"（并发 {self.concurrency}，间隔 {self.probe_delay:g} 秒，约需 "
                f"{max(1, len(targets) * (self.probe_delay + 1.5) / max(1, self.concurrency)) / 60:.1f} 分钟）"
            )
            previous = {}  # 映射可能已变，旧结果不沿用
        else:
            log(f"增量探测 {len(targets)}/{len(listeners)} 个节点（上次可用 + 轮换抽样）")
        hints = self.recent_hints()
        now = time.time()
        timestamp = datetime.now(timezone.utc).isoformat()
        results: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(self.concurrency)
        counter = {"done": 0}

        async def probe_one(item: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
            async with semaphore:
                if self.probe_delay:
                    await asyncio.sleep(self.probe_delay)
                status, detail = await self.probe(int(item["port"]))
            counter["done"] += 1
            if counter["done"] % 20 == 0:
                log(f"  已探测 {counter['done']}/{len(targets)}")
            return item, status, detail

        for item, status, detail in await asyncio.gather(*(probe_one(item) for item in targets)):
            node = str(item["node"])
            hinted = float(hints.get(node) or 0)
            results[str(item["port"])] = {
                "name": str(item["name"]),
                "node": node,
                "port": int(item["port"]),
                "status": status,
                "detail": detail,
                "checked_at": timestamp,
                "hinted_failure_at": hinted if hinted and now - hinted < self.interval_seconds * 3 else 0,
            }

        nodes: list[dict[str, Any]] = []
        for item in listeners:
            port = str(item["port"])
            fresh = results.get(port)
            if fresh:
                nodes.append(fresh)
                continue
            old = previous.get(port)
            nodes.append(
                old
                if old
                else {
                    "name": str(item["name"]),
                    "node": str(item["node"]),
                    "port": int(item["port"]),
                    "status": 0,
                    "detail": "未探测",
                    "checked_at": timestamp,
                    "hinted_failure_at": 0,
                }
            )
        healthy = [item for item in nodes if item["status"] in OK_STATUSES]
        payload = {
            "checked_at": timestamp,
            "official_url": self.official_url,
            "healthy_count": len(healthy),
            "bad_cursor": int(previous_raw.get("bad_cursor") or 0),
            "nodes": nodes,
        }
        write_json(self.state_path, payload)
        probed_bad = [
            f"{item['node']}({item['status'] or item['detail']})"
            for port, item in results.items()
            if item["status"] not in OK_STATUSES
        ]
        log(
            f"本轮探测 {len(targets)}/{len(nodes)} 个；可用 {len(healthy)}"
            + (f"；本轮失败：{', '.join(probed_bad)}" if probed_bad else "")
        )
        return payload

    def run(self) -> None:
        log(
            f"节点健康检查启动：每 {self.interval_seconds / 60:g} 分钟探测一次 "
            f"{self.official_url}（经 {self.proxy_host} 的 per-node 端口）"
        )
        while not self.stop_requested:
            started = time.monotonic()
            self.last_check_started_at = time.time()
            try:
                asyncio.run(self.check_once())
            except Exception as exc:  # noqa: BLE001 - 长跑服务不应因单次异常退出
                log(f"本轮探测异常：{type(exc).__name__} {exc}")
            remaining = self.interval_seconds - (time.monotonic() - started)
            deadline = time.monotonic() + max(5.0, remaining)
            while not self.stop_requested and time.monotonic() < deadline:
                if self.subscription_updated_after(self.last_check_started_at):
                    log("检测到订阅已更新，提前重新探测")
                    break
                time.sleep(min(5, deadline - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐节点探测官网可达性（首次全量，之后增量+轮换）")
    parser.add_argument("--once", action="store_true", help="只探测一轮就退出（供定时调度使用）")
    args = parser.parse_args(argv)
    checker = NodeHealthChecker(
        config_path=Path(os.getenv("NODE_HEALTH_CONFIG", "/config/config.yaml")),
        state_path=Path(os.getenv("NODE_HEALTH_STATE", "/state/nodes.json")),
        hints_path=Path(os.getenv("NODE_HEALTH_HINTS", "/state/poller-hints.json")),
        subscription_path=Path(os.getenv("NODE_HEALTH_SUBSCRIPTION_STATE", "/state/subscription.json")),
        proxy_host=os.getenv("NODE_HEALTH_PROXY_HOST", "mihomo").strip() or "mihomo",
        official_url=os.getenv("NODE_HEALTH_OFFICIAL_URL", "https://www.lovelive-anime.jp/").strip(),
        interval_seconds=env_number("NODE_HEALTH_INTERVAL_MINUTES", 30) * 60,
        timeout=env_number("NODE_HEALTH_TIMEOUT_SECONDS", 30, 5),
        bad_batch=int(env_number("NODE_HEALTH_BAD_BATCH", 6, 0)),
        probe_delay=env_number("NODE_HEALTH_PROBE_DELAY_SECONDS", 0.3, 0),
        concurrency=int(env_number("NODE_HEALTH_CONCURRENCY", 8, 1)),
    )

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        checker.stop_requested = True
        log(f"收到停止信号 {signum}")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if args.once:
        try:
            asyncio.run(checker.check_once())
        except Exception as exc:  # noqa: BLE001 - 一次性模式把失败交给调度器判断
            log(f"探测失败：{type(exc).__name__} {exc}")
            return 1
        return 0
    try:
        checker.run()
    except KeyboardInterrupt:
        checker.stop_requested = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
