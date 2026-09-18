#!/usr/bin/env python3
"""定期拉取代理订阅、重新渲染 mihomo 配置，并让运行中的 mihomo 重载。

- 没配置 ``MIHOMO_SUB_URL`` 时直接退出（视为未启用，不会重启循环）。
- 拉取订阅时优先走现有出口（``MIHOMO_PROXY``），失败再直连。
- 新配置里没有任何匹配节点时**保留旧配置**，避免把可用出口换成空配置。
- 内容无变化时跳过重载；有变化则写回配置文件并调用 mihomo 控制台重载。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logfmt import format_message  # noqa: E402

from mihomo_config import (  # noqa: E402
    DEFAULT_CONTROLLER,
    DEFAULT_GROUP_TYPE,
    GROUP_TYPES,
    DEFAULT_DNS,
    DEFAULT_GROUP,
    DEFAULT_LISTENER_PORT_BASE,
    ConfigError,
    load_proxies,
    render,
    select_nodes,
    validate_group_name,
    write_config,
)

# 订阅商通常按 UA 决定返回格式，用 Clash 系 UA 更稳妥（可用 MIHOMO_SUB_UA 覆盖）
DEFAULT_SUBSCRIPTION_UA = "clash-verge/v1.7.7"
FETCH_TIMEOUT = 60.0
MAX_SUBSCRIPTION_BYTES = 16 * 1024 * 1024
STATE_MAX_BYTES = 1 * 1024 * 1024
HEALTHY_NODE_STATUSES = {200, 206}
REDIRECT_CODES = {301, 302, 303, 307, 308}
# 128 个监听端口热重载在树莓派上约 30 秒，留足余量
RELOAD_TIMEOUT = 180.0


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
    return value if math.isfinite(value) and value >= minimum else default


def split_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_int(name: str, default: int, minimum: int, maximum: int = 65535) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 必须在 {minimum}–{maximum} 范围内")
    return value


def validate_http_url(value: str, field: str) -> str:
    try:
        parsed = urlparse(value)
        parsed.port
        decoded_path = unquote(parsed.path)
        path_parts = decoded_path.split("/")
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 地址格式无效") from exc
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        or "?" in decoded_path
        or "#" in decoded_path
        or "\\" in value
        or "\\" in decoded_path
        or any(part in {".", ".."} for part in path_parts if part)
        or any(not part for part in path_parts[1:-1])
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any(character.isspace() for character in parsed.hostname)
        or parsed.netloc.endswith(":")
        or parsed.port == 0
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "#" in value
        or "?" in value and not parsed.query
        or "#" in value and not parsed.fragment
    ):
        raise ConfigError(f"{field} 必须是无凭据的 HTTP/HTTPS 地址")
    return value


def validate_proxy_url(value: str) -> str:
    parsed_value = validate_http_url(value, "MIHOMO_PROXY")
    parsed = urlparse(parsed_value)
    if parsed.query or "?" in parsed_value or parsed.path not in {"", "/"}:
        raise ConfigError("MIHOMO_PROXY 不能包含路径或查询参数")
    return parsed_value


def effective_http_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def same_http_origin(first: str, second: str) -> bool:
    try:
        first_parsed = urlparse(first)
        second_parsed = urlparse(second)
        first_port = effective_http_port(first_parsed)
        second_port = effective_http_port(second_parsed)
    except (TypeError, ValueError):
        return False
    return (
        first_parsed.scheme == second_parsed.scheme
        and (first_parsed.hostname or "").lower() == (second_parsed.hostname or "").lower()
        and first_port == second_port
    )


def write_state(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise ConfigError("订阅状态目录不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > STATE_MAX_BYTES:
        raise ConfigError(f"订阅状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB 限制")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {}
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return {}
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(STATE_MAX_BYTES + 1)
        if len(raw) > STATE_MAX_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return value if isinstance(value, dict) else {}


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def acquire_update_lock(config_path: Path):
    lock_path = config_path.with_name(f".{config_path.name}.subscription.lock")
    if lock_path.parent.is_symlink():
        raise ConfigError("订阅配置目录不能是符号链接")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock_path.parent, 0o700)
    except OSError:
        pass
    handle = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if handle is not None:
            handle.close()
        raise ConfigError("已有订阅更新任务正在运行") from exc
    return handle


class SubscriptionUpdater:
    def __init__(self, config_path: Path, state_path: Path, staging_path: Path):
        self.config_path = config_path
        self.state_path = state_path
        self.staging_path = staging_path
        try:
            if self.staging_path.resolve() == self.config_path.resolve():
                raise ConfigError("MIHOMO_SUB_STAGING 不能与 mihomo 配置文件相同")
        except OSError as exc:
            raise ConfigError("MIHOMO_SUB_STAGING 路径无法确认") from exc
        self.stop_requested = False
        self.url = os.getenv("MIHOMO_SUB_URL", "").strip()
        if self.url:
            validate_http_url(self.url, "MIHOMO_SUB_URL")
        self.user_agent = os.getenv("MIHOMO_SUB_UA", DEFAULT_SUBSCRIPTION_UA).strip() or DEFAULT_SUBSCRIPTION_UA
        if (
            len(self.user_agent) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.user_agent)
        ):
            raise ConfigError("MIHOMO_SUB_UA 格式无效")
        self.proxy = os.getenv("MIHOMO_PROXY", "").strip()
        if self.proxy:
            self.proxy = validate_proxy_url(self.proxy)
        self.controller = os.getenv("MIHOMO_CONTROLLER", "http://mihomo:9090").strip().rstrip("/")
        try:
            controller_url = urlparse(self.controller)
            controller_url.port
        except (TypeError, ValueError) as exc:
            raise ConfigError("MIHOMO_CONTROLLER 地址格式无效") from exc
        if (
            len(self.controller) > 2048
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.controller)
            or "\\" in self.controller
            or controller_url.scheme not in {"http", "https"}
            or not controller_url.hostname
            or any(character.isspace() for character in controller_url.hostname)
            or controller_url.netloc.endswith(":")
            or controller_url.port == 0
            or controller_url.username
            or controller_url.password
            or controller_url.query
            or "?" in self.controller
            or controller_url.fragment
            or "#" in self.controller
            or controller_url.path not in {"", "/"}
        ):
            raise ConfigError("MIHOMO_CONTROLLER 必须是无凭据且不带查询参数的 HTTP/HTTPS 地址")
        self.controller_secret = os.getenv("MIHOMO_CONTROLLER_SECRET", "").strip()
        if (
            len(self.controller_secret.encode("utf-8")) < 32
            or len(self.controller_secret.encode("utf-8")) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.controller_secret)
        ):
            raise ConfigError("MIHOMO_CONTROLLER_SECRET 必须是 32–256 字节且不含控制字符")
        if not self.controller_secret:
            raise ConfigError("MIHOMO_CONTROLLER_SECRET 未配置，拒绝使用未认证的 mihomo 控制接口")
        # 重载时传给 mihomo 的路径是「mihomo 容器里看到的路径」，与本地写入路径可能不同
        self.remote_config = os.getenv("MIHOMO_CONFIG_REMOTE", str(config_path)).strip() or str(config_path)
        if (
            len(self.remote_config) > 4096
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.remote_config)
            or "\\" in self.remote_config
            or ".." in Path(self.remote_config).parts
        ):
            raise ConfigError("MIHOMO_CONFIG_REMOTE 路径格式无效")
        self.interval_seconds = env_number("MIHOMO_SUB_INTERVAL_MINUTES", 720) * 60
        self.includes = split_list("MIHOMO_INCLUDE", "")
        self.excludes = split_list("MIHOMO_EXCLUDE", "将在")
        self.port = env_int("MIHOMO_PORT", 7890, 1)
        self.listener_port_base = env_int("MIHOMO_LISTENER_PORT_BASE", DEFAULT_LISTENER_PORT_BASE, 1)
        self.controller_listen = os.getenv("MIHOMO_CONTROLLER_LISTEN", DEFAULT_CONTROLLER).strip()
        self.group = validate_group_name(os.getenv("MIHOMO_GROUP", DEFAULT_GROUP).strip() or DEFAULT_GROUP)
        self.dns = tuple(split_list("MIHOMO_DNS", ",".join(DEFAULT_DNS)))
        self.group_type = os.getenv("MIHOMO_GROUP_TYPE", DEFAULT_GROUP_TYPE).strip() or DEFAULT_GROUP_TYPE
        if self.group_type not in GROUP_TYPES:
            log(f"MIHOMO_GROUP_TYPE 不支持 {self.group_type}，改用 {DEFAULT_GROUP_TYPE}")
            self.group_type = DEFAULT_GROUP_TYPE
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
                with httpx.Client(
                    proxy=proxy,
                    timeout=FETCH_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    target = self.url
                    initial_parts = urlparse(self.url)
                    initial_host = initial_parts.hostname
                    for _ in range(5):
                        with client.stream(
                            "GET",
                            target,
                            headers={"User-Agent": self.user_agent, "Accept": "text/yaml,text/plain,*/*"},
                        ) as response:
                            if response.status_code in REDIRECT_CODES:
                                location = response.headers.get("location", "")
                                if not location:
                                    raise ConfigError("订阅重定向缺少目标地址")
                                target = validate_http_url(urljoin(target, location), "MIHOMO_SUB_URL")
                                target_parts = urlparse(target)
                                if (
                                    target_parts.hostname != initial_host
                                    or target_parts.scheme != initial_parts.scheme
                                    or effective_http_port(target_parts) != effective_http_port(initial_parts)
                                ):
                                    raise ConfigError("订阅不得重定向到其它主机、端口或降级协议")
                                continue
                            response.raise_for_status()
                            content_length = response.headers.get("Content-Length", "")
                            try:
                                if content_length and int(content_length) > MAX_SUBSCRIPTION_BYTES:
                                    raise ConfigError("订阅内容超过 16 MB 限制")
                            except ValueError:
                                pass
                            content = bytearray()
                            for chunk in response.iter_bytes():
                                remaining = MAX_SUBSCRIPTION_BYTES - len(content)
                                if len(chunk) > remaining:
                                    raise ConfigError("订阅内容超过 16 MB 限制")
                                content.extend(chunk)
                            break
                    else:
                        raise ConfigError("订阅重定向次数过多")
                try:
                    text = bytes(content).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ConfigError("订阅内容不是有效的 UTF-8") from exc
                if len(text) < 100:
                    raise ConfigError("订阅内容过短")
                log(f"订阅拉取成功（{label}）：{len(text)} 字节")
                return text
            except (httpx.HTTPError, ConfigError, OSError) as exc:
                last_error = f"{label} {type(exc).__name__}"
                log(f"订阅拉取失败（{label}）：{type(exc).__name__}")
        raise ConfigError(f"订阅拉取失败：{last_error}")

    def reload_mihomo(self) -> None:
        """让 mihomo 重载配置。端口多时热重载较慢（Pi 上 128 个监听约 30 秒），所以放宽超时并重试。"""
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                headers = (
                    {"Authorization": f"Bearer {self.controller_secret}"}
                    if self.controller_secret
                    else {}
                )
                response = httpx.put(
                    f"{self.controller}/configs",
                    params={"force": "true"},
                    json={"path": self.remote_config},
                    headers=headers,
                    timeout=RELOAD_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                )
                response.raise_for_status()
                log("已通知 mihomo 重载配置")
                return
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                detail = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                log(f"重载失败（第 {attempt}/3 次）：{detail}")
                if attempt < 3:
                    time.sleep(10)
        raise ConfigError(f"重载失败：{last_error}")

    @staticmethod
    def healthy_names(path: Path, config_path: Path | None = None) -> set[str]:
        """从健康检查产出的 nodes.json 里取状态为 200 的节点名。"""
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return set()
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(STATE_MAX_BYTES + 1)
            if len(raw) > STATE_MAX_BYTES:
                return set()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return set()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if config_path is not None:
            if not config_path.is_file():
                return set()
            expected_digest = str(payload.get("config_sha256") or "") if isinstance(payload, dict) else ""
            if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                return set()
            digest_descriptor = -1
            try:
                digest_descriptor = os.open(config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                if not stat.S_ISREG(os.fstat(digest_descriptor).st_mode):
                    return set()
                digest = hashlib.sha256()
                while chunk := os.read(digest_descriptor, 64 * 1024):
                    digest.update(chunk)
                actual_digest = digest.hexdigest()
            except OSError:
                return set()
            finally:
                if digest_descriptor >= 0:
                    os.close(digest_descriptor)
            if expected_digest != actual_digest:
                return set()
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        return {
            str(item.get("node"))
            for item in (nodes or [])
            if isinstance(item, dict) and item.get("status") in HEALTHY_NODE_STATUSES and item.get("node")
        }

    # ---------- 单轮 ----------

    def update_once(self) -> None:
        lock = acquire_update_lock(self.config_path)
        try:
            self._update_once()
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()

    def _update_once(self) -> None:
        text = self.fetch_subscription()
        self.staging_path.parent.mkdir(parents=True, exist_ok=True)
        # 订阅内容通常包含节点凭据；用 600 权限原子写入临时文件，避免
        # 在解析期间被同机其它用户读取。
        try:
            write_config(self.staging_path, text)
            try:
                proxies = load_proxies(self.staging_path)
                names = select_nodes(proxies, self.includes, self.excludes)
                healthy_file = os.getenv("MIHOMO_HEALTHY_FILE", "").strip()
                if healthy_file and Path(healthy_file).exists():
                    # 优先只保留健康检查确认可用的节点（未命中时回退到地区过滤）
                    healthy = self.healthy_names(Path(healthy_file), self.config_path)
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
        previous_available = False
        try:
            previous = self.config_path.read_text(encoding="utf-8")
            previous_available = True
        except (OSError, UnicodeDecodeError) as exc:
            if self.config_path.exists():
                raise ConfigError("无法读取现有 mihomo 配置，拒绝覆盖") from exc
        rendered_digest = text_digest(rendered)
        state = read_state(self.state_path)
        if previous == rendered and state.get("config_sha256") == rendered_digest:
            log(f"订阅无变化，跳过重载（{len(names)} 个节点）")
            write_state(
                self.state_path,
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "changed": False,
                    "reloaded": False,
                    "config_sha256": rendered_digest,
                    "node_count": len(names),
                    "nodes": names,
                },
            )
            return

        # 只有控制台确认接受后才把这份配置记为已加载。若重载超时或失败，
        # 立即恢复磁盘上的旧配置，避免 mihomo 下次重启时读到未确认的新配置。
        previous_exists = self.config_path.exists()
        write_config(self.config_path, rendered)
        try:
            self.reload_mihomo()
        except Exception:
            try:
                if previous_available:
                    write_config(self.config_path, previous)
                elif not previous_exists:
                    self.config_path.unlink(missing_ok=True)
            except Exception:
                log("重载失败后无法恢复旧配置")
            raise
        write_state(
            self.state_path,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "changed": previous != rendered,
                "reloaded": True,
                "config_sha256": rendered_digest,
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
    parser.add_argument("--once", action="store_true", help="只更新一次就退出（供定时调度使用）")
    args = parser.parse_args(argv)
    if not os.getenv("MIHOMO_SUB_URL", "").strip():
        log("未配置 MIHOMO_SUB_URL，订阅自动更新未启用")
        return 0
    try:
        updater = SubscriptionUpdater(
            config_path=Path(os.getenv("MIHOMO_CONFIG_OUT", "/config/config.yaml")),
            state_path=Path(os.getenv("MIHOMO_SUB_STATE", "/state/subscription.json")),
            staging_path=Path(os.getenv("MIHOMO_SUB_STAGING", "/tmp/subscription-staging.yaml")),
        )
    except ConfigError as exc:
        log(f"配置错误：{exc}")
        return 2

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        updater.stop_requested = True
        log(f"收到停止信号 {signum}")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if args.once:
        try:
            updater.update_once()
        except Exception as exc:  # noqa: BLE001 - 一次性模式把失败交给调度器判断
            log(f"更新失败：{type(exc).__name__} {exc}")
            return 1
        return 0
    try:
        updater.run()
    except KeyboardInterrupt:
        updater.stop_requested = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
