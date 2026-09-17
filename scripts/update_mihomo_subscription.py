#!/usr/bin/env python3
"""定期拉取代理订阅、重新渲染 mihomo 配置，并让运行中的 mihomo 重载。

- 没配置 ``MIHOMO_SUB_URL`` 时直接退出（视为未启用，不会重启循环）。
- 拉取订阅时优先走现有出口（``MIHOMO_PROXY``），失败再直连。
- 新配置里没有任何匹配节点时**保留旧配置**，避免把可用出口换成空配置。
- 内容无变化时跳过重载；有变化则写回配置文件并调用 mihomo 控制台重载。
"""

from __future__ import annotations

import argparse
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

from mihomo_config import (  # noqa: E402
    DEFAULT_CONTROLLER,
    GROUP_TYPES,
    read_node_list,
    select_exact,
    DEFAULT_DNS,
    DEFAULT_GROUP,
    DEFAULT_LISTENER_PORT_BASE,
    ConfigError,
    load_proxies,
    render,
    select_nodes,
    write_config,
)

# 订阅商通常按 UA 决定返回格式，用 Clash 系 UA 更稳妥（可用 MIHOMO_SUB_UA 覆盖）
DEFAULT_SUBSCRIPTION_UA = "clash-verge/v1.7.7"
FETCH_TIMEOUT = 60.0


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


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


def split_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class SubscriptionUpdater:
    def __init__(self, config_path: Path, state_path: Path, staging_path: Path):
        self.config_path = config_path
        self.state_path = state_path
        self.staging_path = staging_path
        self.stop_requested = False
        self.url = os.getenv("MIHOMO_SUB_URL", "").strip()
        self.user_agent = os.getenv("MIHOMO_SUB_UA", DEFAULT_SUBSCRIPTION_UA).strip() or DEFAULT_SUBSCRIPTION_UA
        self.proxy = os.getenv("MIHOMO_PROXY", "").strip()
        self.controller = os.getenv("MIHOMO_CONTROLLER", "http://mihomo:9090").strip().rstrip("/")
        # 重载时传给 mihomo 的路径是「mihomo 容器里看到的路径」，与本地写入路径可能不同
        self.remote_config = os.getenv("MIHOMO_CONFIG_REMOTE", str(config_path)).strip() or str(config_path)
        self.interval_seconds = env_number("MIHOMO_SUB_INTERVAL_MINUTES", 720) * 60
        self.includes = split_list("MIHOMO_INCLUDE", "")
        self.excludes = split_list("MIHOMO_EXCLUDE", "将在")
        self.port = int(env_number("MIHOMO_PORT", 7890))
        self.listener_port_base = int(env_number("MIHOMO_LISTENER_PORT_BASE", DEFAULT_LISTENER_PORT_BASE))
        self.controller_listen = os.getenv("MIHOMO_CONTROLLER_LISTEN", DEFAULT_CONTROLLER).strip()
        self.group = os.getenv("MIHOMO_GROUP", DEFAULT_GROUP).strip() or DEFAULT_GROUP
        self.dns = tuple(split_list("MIHOMO_DNS", ",".join(DEFAULT_DNS)))
        self.group_type = os.getenv("MIHOMO_GROUP_TYPE", "select").strip() or "select"
        if self.group_type not in GROUP_TYPES:
            log(f"MIHOMO_GROUP_TYPE 不支持 {self.group_type}，改用 select")
            self.group_type = "select"
        self.cn_direct = os.getenv("MIHOMO_CN_DIRECT", "").strip().lower() in {"1", "true", "yes", "on"}
        self.listeners_enabled = os.getenv("MIHOMO_LISTENERS", "1").strip().lower() not in {"0", "false", "no", "off"}

    # ---------- 网络 ----------

    def fetch_subscription(self) -> str:
        # 直连优先：部分订阅商按请求来源 IP 返回不同的节点列表（经代理可能只拿到子集）
        attempts: list[tuple[str, str | None]] = [("直连", None)]
        if self.proxy:
            attempts.append(("经现有出口", self.proxy))
        last_error = "未知错误"
        for label, proxy in attempts:
            try:
                with httpx.Client(proxy=proxy, timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                    response = client.get(
                        self.url,
                        headers={"User-Agent": self.user_agent, "Accept": "text/yaml,text/plain,*/*"},
                    )
                response.raise_for_status()
                if len(response.text) < 100:
                    raise ConfigError("订阅内容过短")
                log(f"订阅拉取成功（{label}）：{len(response.text)} 字节")
                return response.text
            except (httpx.HTTPError, ConfigError, OSError) as exc:
                last_error = f"{label} {type(exc).__name__}"
                log(f"订阅拉取失败（{label}）：{type(exc).__name__}")
        raise ConfigError(f"订阅拉取失败：{last_error}")

    def reload_mihomo(self) -> None:
        response = httpx.put(
            f"{self.controller}/configs",
            params={"force": "true"},
            json={"path": self.remote_config},
            timeout=30,
        )
        response.raise_for_status()
        log("已通知 mihomo 重载配置")

    @staticmethod
    def healthy_names(path: Path) -> set[str]:
        """从健康检查产出的 nodes.json 里取状态为 200 的节点名。"""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return set()
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        return {
            str(item.get("node"))
            for item in (nodes or [])
            if isinstance(item, dict) and item.get("status") == 200 and item.get("node")
        }

    # ---------- 单轮 ----------

    def update_once(self) -> None:
        text = self.fetch_subscription()
        self.staging_path.parent.mkdir(parents=True, exist_ok=True)
        self.staging_path.write_text(text, encoding="utf-8")
        try:
            proxies = load_proxies(self.staging_path)
            names = select_nodes(proxies, self.includes, self.excludes)
            healthy_file = os.getenv("MIHOMO_HEALTHY_FILE", "").strip()
            if healthy_file and Path(healthy_file).exists():
                # 优先只保留健康检查确认可用的节点（未命中时回退到地区过滤）
                healthy = self.healthy_names(Path(healthy_file))
                filtered = [name for name in names if name in healthy]
                if filtered:
                    log(f"按健康名单过滤：{len(filtered)}/{len(names)} 个节点可用")
                    names = filtered
                else:
                    log("健康名单里没有匹配节点，本轮保留地区过滤结果")
            if not names:
                raise ConfigError(f"订阅里没有匹配节点（include={self.includes} exclude={self.excludes}）")
            rendered = render(
                names,
                proxies,
                port=self.port,
                controller=self.controller_listen,
                dns=self.dns,
                group=self.group,
                listener_port_base=self.listener_port_base,
                listeners_enabled=self.listeners_enabled,
                group_type=self.group_type,
                cn_direct=self.cn_direct,
            )
        except ConfigError as exc:
            log(f"本轮跳过（保留旧配置）：{exc}")
            return
        finally:
            try:
                self.staging_path.unlink()
            except OSError:
                pass

        previous = ""
        try:
            previous = self.config_path.read_text(encoding="utf-8")
        except OSError:
            pass
        if previous == rendered:
            log(f"订阅无变化，跳过重载（{len(names)} 个节点）")
            write_state(
                self.state_path,
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "changed": False,
                    "node_count": len(names),
                    "nodes": names,
                },
            )
            return

        write_config(self.config_path, rendered)
        self.reload_mihomo()
        write_state(
            self.state_path,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "changed": True,
                "node_count": len(names),
                "nodes": names,
            },
        )
        shape = f"组类型 {self.group_type}" + ("，国内直连" if self.cn_direct else "")
        shape += "，per-node 端口" if self.listeners_enabled else "，单端口"
        log(f"配置已更新：{len(names)} 个节点（{shape}）→ {self.config_path}")

    def run(self) -> None:
        log(f"订阅自动更新启动：每 {self.interval_seconds / 60:g} 分钟检查一次")
        while not self.stop_requested:
            started = time.monotonic()
            try:
                self.update_once()
            except Exception as exc:  # noqa: BLE001 - 长跑服务不应因单次异常退出
                log(f"本轮异常：{type(exc).__name__} {exc}")
            remaining = self.interval_seconds - (time.monotonic() - started)
            deadline = time.monotonic() + max(30.0, remaining)
            while not self.stop_requested and time.monotonic() < deadline:
                time.sleep(min(10, deadline - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定期更新 mihomo 订阅配置")
    parser.parse_args(argv)
    if not os.getenv("MIHOMO_SUB_URL", "").strip():
        log("未配置 MIHOMO_SUB_URL，订阅自动更新未启用")
        return 0
    updater = SubscriptionUpdater(
        config_path=Path(os.getenv("MIHOMO_CONFIG_OUT", "/config/config.yaml")),
        state_path=Path(os.getenv("MIHOMO_SUB_STATE", "/state/subscription.json")),
        staging_path=Path(os.getenv("MIHOMO_SUB_STAGING", "/tmp/subscription-staging.yaml")),
    )

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        updater.stop_requested = True
        log(f"收到停止信号 {signum}")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        updater.run()
    except KeyboardInterrupt:
        updater.stop_requested = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
