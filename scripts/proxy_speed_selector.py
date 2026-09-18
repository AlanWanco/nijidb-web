#!/usr/bin/env python3
"""按实际下载吞吐为 mihomo 共享代理组选路。

mihomo 的 ``url-test`` 主要比较请求延迟，不能反映节点的实际下载速度。这个任务只对少量
候选节点顺序下载一个小的固定测试文件，然后通过控制接口选择吞吐最高的节点。它不访问官网，
也不参与官网轮询的节点轮换；共享组仍使用 ``fallback``，因此手动选择在重启之外不会被延迟
探测覆盖，节点失效时仍可自动故障切换。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

DOH_RESOLVERS = (("1.1.1.1", "cloudflare-dns.com"), ("8.8.8.8", "dns.google"))
DOH_TIMEOUT_SECONDS = 5
DOH_MAX_RESPONSE_BYTES = 64 * 1024
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logfmt import format_message  # noqa: E402
from mihomo_config import ConfigError, read_listeners, validate_group_name, validate_proxy_host  # noqa: E402

DEFAULT_SPEED_URL = "https://speed.cloudflare.com/__down?bytes=524288"
DEFAULT_GROUP = "PROXY"
DEFAULT_CONTROLLER = "http://mihomo:9090"
DEFAULT_PROXY_HOST = "mihomo"
DEFAULT_CANDIDATE_COUNT = 8
DEFAULT_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MIN_GAIN = 0.15
STATE_MAX_BYTES = 4 * 1024 * 1024


class SpeedSelectorError(RuntimeError):
    """测速或切换共享出口失败。"""


@dataclass(frozen=True)
class SpeedResult:
    name: str
    port: int
    bytes_read: int = 0
    elapsed_seconds: float = 0.0
    error: str = ""

    @property
    def speed_bps(self) -> float:
        if self.error or self.bytes_read <= 0 or self.elapsed_seconds <= 0:
            return 0.0
        return self.bytes_read / self.elapsed_seconds


def log(message: str) -> None:
    print(format_message(f"[proxy-speed] {message}"), flush=True)


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SpeedSelectorError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum or maximum is not None and value > maximum:
        bound = f"{minimum}–{maximum}" if maximum is not None else f"不小于 {minimum}"
        raise SpeedSelectorError(f"环境变量 {name} 必须在 {bound} 范围内")
    return value


def env_float(name: str, default: float, minimum: float = 0, maximum: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SpeedSelectorError(f"环境变量 {name} 必须是数字") from exc
    if not math.isfinite(value) or value < minimum or maximum is not None and value > maximum:
        bound = f"{minimum:g}–{maximum:g}" if maximum is not None else f"不小于 {minimum:g}"
        raise SpeedSelectorError(f"环境变量 {name} 必须在 {bound} 范围内")
    return value


def read_json(path: Path) -> dict[str, Any]:
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > STATE_MAX_BYTES:
        raise SpeedSelectorError(f"测速状态文件超过 {STATE_MAX_BYTES // (1024 * 1024)} MB")
    if path.parent.is_symlink():
        raise SpeedSelectorError("测速状态目录不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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


def config_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise SpeedSelectorError("无法读取 mihomo 配置") from exc


def acquire_config_lock(config_path: Path):
    lock_path = config_path.with_name(f".{config_path.name}.subscription.lock")
    if lock_path.parent.is_symlink():
        raise SpeedSelectorError("订阅配置目录不能是符号链接")
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
        raise SpeedSelectorError("订阅配置正在更新，跳过本次测速") from exc
    return handle


def acquire_run_lock(state_path: Path):
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    if lock_path.parent.is_symlink():
        raise SpeedSelectorError("测速状态目录不能是符号链接")
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
        raise SpeedSelectorError("已有测速任务正在运行") from exc
    return handle


def valid_http_url(value: str, field: str) -> str:
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
        decoded_path = unquote(parsed.path)
        path_parts = decoded_path.split("/")
    except (TypeError, ValueError) as exc:
        raise SpeedSelectorError(f"{field}必须是无凭据的 HTTP/HTTPS 地址") from exc
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
        or not hostname
        or any(character.isspace() for character in hostname)
        or parsed.netloc.endswith(":")
        or parsed.username
        or parsed.password
        or parsed.port is not None and not 1 <= parsed.port <= 65535
        or parsed.fragment
        or "#" in value
        or "?" in value and not parsed.query
    ):
        raise SpeedSelectorError(f"{field}必须是无凭据的 HTTP/HTTPS 地址")
    return value


def controller_url(value: str) -> str:
    validated = valid_http_url(value, "MIHOMO_SPEED_CONTROLLER")
    parsed = urlparse(validated)
    if parsed.query or "?" in validated or parsed.fragment or "#" in validated or parsed.path not in {"", "/"}:
        raise SpeedSelectorError("MIHOMO_SPEED_CONTROLLER 不能包含路径或查询参数")
    return validated.rstrip("/")


def recent_delay(proxy: dict[str, Any]) -> int | None:
    history = proxy.get("history")
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        try:
            delay = int(item.get("delay") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if delay > 0:
            return delay
    return None


def choose_selected(results: list[SpeedResult], current: str, min_gain: float) -> SpeedResult:
    successful = [result for result in results if result.speed_bps > 0]
    if not successful:
        raise SpeedSelectorError("候选节点均未完成测速")
    fastest = max(successful, key=lambda result: result.speed_bps)
    current_result = next((result for result in successful if result.name == current), None)
    if (
        current_result
        and fastest.name != current
        and fastest.speed_bps < current_result.speed_bps * (1 + min_gain)
    ):
        return current_result
    return fastest


class ProxySpeedSelector:
    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        nodes_path: Path,
        controller: str,
        group: str,
        proxy_host: str,
        speed_url: str,
        candidate_count: int,
        bytes_to_read: int,
        timeout_seconds: float,
        min_gain: float,
        start_delay_seconds: float,
        controller_secret: str = "",
    ):
        if controller_secret and (
            len(controller_secret.encode("utf-8")) < 32
            or len(controller_secret.encode("utf-8")) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in controller_secret)
        ):
            raise SpeedSelectorError("mihomo 控制密钥必须是 32–256 字节且不含控制字符")
        self.config_path = config_path
        self.state_path = state_path
        self.nodes_path = nodes_path
        self.controller = controller_url(controller)
        try:
            self.group = validate_group_name(group)
        except ConfigError as exc:
            raise SpeedSelectorError(str(exc)) from exc
        try:
            self.proxy_host = validate_proxy_host(proxy_host, "MIHOMO_SPEED_PROXY_HOST")
        except ConfigError as exc:
            raise SpeedSelectorError(str(exc)) from exc
        self.speed_url = speed_url
        self.candidate_count = candidate_count
        self.bytes_to_read = bytes_to_read
        self.timeout_seconds = timeout_seconds
        self.min_gain = min_gain
        self.start_delay_seconds = start_delay_seconds
        self.controller_secret = controller_secret

    def controller_headers(self) -> dict[str, str]:
        if not self.controller_secret:
            return {}
        return {"Authorization": f"Bearer {self.controller_secret}"}

    async def controller_json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        try:
            response = await client.get(f"{self.controller}{path}")
        except (httpx.HTTPError, OSError) as exc:
            raise SpeedSelectorError("mihomo 控制接口不可用") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise SpeedSelectorError(f"mihomo 控制接口 HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as exc:
            raise SpeedSelectorError("mihomo 控制接口返回格式无效") from exc
        if not isinstance(value, dict):
            raise SpeedSelectorError("mihomo 控制接口返回格式无效")
        return value

    async def group_info(self, client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
        catalog = await self.controller_json(client, "/proxies")
        proxies = catalog.get("proxies")
        group = proxies.get(self.group) if isinstance(proxies, dict) else None
        if not isinstance(group, dict):
            raise SpeedSelectorError(f"mihomo 中不存在代理组：{self.group}")
        names = group.get("all")
        if not isinstance(names, list):
            raise SpeedSelectorError("mihomo 代理组没有节点列表")
        return group, {str(name): value for name, value in proxies.items() if isinstance(value, dict)}

    def listeners(self) -> dict[str, int]:
        try:
            return {str(item["node"]): int(item["port"]) for item in read_listeners(self.config_path)}
        except (ConfigError, TypeError, ValueError) as exc:
            raise SpeedSelectorError("无法读取 mihomo per-node 入口") from exc

    def healthy_names(self) -> set[str]:
        payload = read_json(self.nodes_path)
        if self.config_path.is_file():
            expected_digest = str(payload.get("config_sha256") or "")
            if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
                return set()
            try:
                if expected_digest != config_digest(self.config_path):
                    return set()
            except SpeedSelectorError:
                return set()
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            return set()
        return {
            str(item.get("node"))
            for item in nodes
            if isinstance(item, dict) and item.get("status") in {200, 206} and item.get("node")
        }

    def candidate_names(
        self,
        group: dict[str, Any],
        proxy_catalog: dict[str, dict[str, Any]],
        listener_ports: dict[str, int],
    ) -> list[str]:
        all_names = [str(name) for name in group.get("all", []) if str(name) in listener_ports]
        if not all_names:
            raise SpeedSelectorError("代理组中没有可测速的 per-node 节点")
        healthy = self.healthy_names()
        eligible = [name for name in all_names if not healthy or name in healthy]
        if not eligible:
            eligible = all_names
        current = str(group.get("now") or "")
        previous = str(read_json(self.state_path).get("selected") or "")
        priority = [name for name in (current, previous) if name in all_names]
        positions = {name: index for index, name in enumerate(all_names)}
        ranked = sorted(
            eligible,
            key=lambda name: (
                recent_delay(proxy_catalog.get(name, {})) is None,
                recent_delay(proxy_catalog.get(name, {})) or 0,
                positions[name],
            ),
        )
        ordered = [name for name in (*priority, *ranked) if name in eligible or name in priority]
        result: list[str] = []
        for name in ordered:
            if name not in result:
                result.append(name)
            if len(result) >= self.candidate_count:
                break
        return result

    async def measure(self, name: str, port: int) -> SpeedResult:
        started = time.perf_counter()
        total = 0
        try:
            timeout = httpx.Timeout(self.timeout_seconds)
            async with httpx.AsyncClient(
                proxy=f"http://{self.proxy_host}:{port}",
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    self.speed_url,
                    headers={"Accept": "application/octet-stream", "Cache-Control": "no-cache"},
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        return SpeedResult(name, port, error=f"HTTP {response.status_code}")
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        remaining = self.bytes_to_read - total
                        if remaining <= 0:
                            break
                        total += len(chunk[:remaining])
                        if total >= self.bytes_to_read:
                            break
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return SpeedResult(name, port, error=type(exc).__name__)
        elapsed = max(time.perf_counter() - started, 0.001)
        if total <= 0:
            return SpeedResult(name, port, elapsed_seconds=elapsed, error="empty response")
        return SpeedResult(name, port, total, elapsed)

    async def select_proxy(self) -> tuple[str, list[SpeedResult], str]:
        if not self.controller_secret:
            raise SpeedSelectorError("MIHOMO_CONTROLLER_SECRET 未配置，拒绝使用未认证的 mihomo 控制接口")
        config_lock = acquire_config_lock(self.config_path)
        try:
            listener_ports = self.listeners()
            before = config_digest(self.config_path)
            timeout = httpx.Timeout(self.timeout_seconds)
            async with httpx.AsyncClient(
                timeout=timeout,
                trust_env=False,
                headers=self.controller_headers(),
            ) as controller_client:
                group, catalog = await self.group_info(controller_client)
                current = str(group.get("now") or "")
                names = self.candidate_names(group, catalog, listener_ports)
                log(f"开始顺序测速：{len(names)} 个候选节点")
                results: list[SpeedResult] = []
                for name in names:
                    result = await self.measure(name, listener_ports[name])
                    results.append(result)
                    if result.speed_bps:
                        log(f"  {name}：{result.speed_bps / 1024:.1f} KiB/s")
                    else:
                        log(f"  {name}：失败（{result.error}）")

                after = config_digest(self.config_path)
                if before != after:
                    raise SpeedSelectorError("测速期间 mihomo 配置发生变化，放弃本次切换")
                selected = choose_selected(results, current, self.min_gain)
                group_after, _ = await self.group_info(controller_client)
                if config_digest(self.config_path) != before:
                    raise SpeedSelectorError("切换前 mihomo 配置发生变化，放弃本次切换")
                if selected.name not in {str(name) for name in group_after.get("all", [])}:
                    raise SpeedSelectorError("最快节点已不在当前代理组，放弃本次切换")
                if selected.name != str(group_after.get("now") or ""):
                    try:
                        response = await controller_client.put(
                            f"{self.controller}/proxies/{quote(self.group, safe='')}",
                            json={"name": selected.name},
                        )
                    except (httpx.HTTPError, OSError) as exc:
                        raise SpeedSelectorError("切换 mihomo 代理组失败") from exc
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SpeedSelectorError(f"切换 mihomo 代理组 HTTP {response.status_code}")
                    log(f"共享代理组已切换：{group_after.get('now') or '未知'} → {selected.name}")
                else:
                    log(f"共享代理组保持：{selected.name}")
                return selected.name, results, before
        finally:
            try:
                fcntl.flock(config_lock.fileno(), fcntl.LOCK_UN)
            finally:
                config_lock.close()

    def run_once(self) -> int:
        try:
            lock = acquire_run_lock(self.state_path)
        except SpeedSelectorError as exc:
            log(f"测速任务失败：{exc}")
            return 1
        try:
            if self.start_delay_seconds:
                time.sleep(self.start_delay_seconds)
            try:
                selected, results, digest = asyncio.run(self.select_proxy())
            except SpeedSelectorError as exc:
                log(f"测速任务失败：{exc}")
                return 1
            write_json(
                self.state_path,
                {
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "selected": selected,
                    "config_sha256": digest,
                    "results": [
                        {
                            "node": result.name,
                            "port": result.port,
                            "bytes": result.bytes_read,
                            "elapsed_seconds": round(result.elapsed_seconds, 3),
                            "speed_bps": round(result.speed_bps, 2),
                            "error": result.error,
                        }
                        for result in results
                    ],
                },
            )
            return 0
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="顺序测速并选择 mihomo 共享代理出口")
    parser.add_argument("--once", action="store_true", help="只测速并选择一次")
    return parser.parse_args(argv)


def _required_controller_secret() -> str:
    secret = os.getenv("MIHOMO_CONTROLLER_SECRET", "").strip()
    if not secret:
        raise SpeedSelectorError("MIHOMO_CONTROLLER_SECRET 未配置，拒绝使用未认证的 mihomo 控制接口")
    if (
        len(secret.encode("utf-8")) < 32
        or len(secret.encode("utf-8")) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in secret)
    ):
        raise SpeedSelectorError("mihomo 控制密钥必须是 32–256 字节且不含控制字符")
    return secret


def canonical_hostname(host: str) -> str:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized or len(normalized) > 253 or any(character.isspace() for character in normalized):
        return ""
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return normalized
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        return ""
    return normalized


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _legacy_ipv4(host: str):
    # inet_aton accepts legacy one-, two-, three- and hexadecimal-component
    # IPv4 forms such as 127.1 and 0x7f000001. They are rejected outright so
    # validation and the eventual proxy resolver cannot disagree.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def speed_host_is_safe(host: str) -> bool:
    normalized = canonical_hostname(host)
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith((".local", ".internal")):
        return False
    address = _literal_ip(normalized)
    if address is not None:
        return address.is_global
    if _legacy_ipv4(normalized) is not None:
        return False
    return "." in normalized and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)


def _doh_query(
    host: str, record_type: str, resolver_ip: str, resolver_name: str
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    path = f"/dns-query?name={quote(host, safe='')}&type={record_type}"
    raw_socket = None
    tls_socket = None
    try:
        raw_socket = socket.create_connection((resolver_ip, 443), timeout=DOH_TIMEOUT_SECONDS)
        context = ssl.create_default_context()
        tls_socket = context.wrap_socket(raw_socket, server_hostname=resolver_name)
        raw_socket = None
        tls_socket.settimeout(DOH_TIMEOUT_SECONDS)
        tls_socket.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {resolver_name}\r\n"
                "Accept: application/dns-json\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(tls_socket, method="GET")
        response.begin()
        if response.status != 200:
            raise OSError(f"DoH HTTP {response.status}")
        payload = response.read(DOH_MAX_RESPONSE_BYTES + 1)
        if len(payload) > DOH_MAX_RESPONSE_BYTES:
            raise OSError("DoH 响应过大")
        decoded = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ssl.SSLError) as exc:
        raise OSError("DoH 查询失败") from exc
    finally:
        if tls_socket is not None:
            tls_socket.close()
        if raw_socket is not None:
            raw_socket.close()
    answers = decoded.get("Answer") if isinstance(decoded, dict) else None
    if not isinstance(answers, list):
        return []
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    expected_type = 1 if record_type == "A" else 28
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != expected_type:
            continue
        try:
            addresses.append(ipaddress.ip_address(str(answer.get("data") or "")))
        except ValueError:
            raise OSError("DoH 地址格式无效")
    return addresses


def _doh_resolve_global(host: str) -> bool:
    """Resolve through pinned DoH when the local network supplies mihomo fake IPs."""
    for resolver_ip, resolver_name in DOH_RESOLVERS:
        try:
            addresses = _doh_query(host, "A", resolver_ip, resolver_name)
            addresses.extend(_doh_query(host, "AAAA", resolver_ip, resolver_name))
        except OSError:
            continue
        if addresses:
            return all(address.is_global for address in addresses)
    return False


def speed_host_resolves_global(host: str) -> bool:
    """Reject DNS names that currently resolve to any private/non-global address."""
    address = _literal_ip(host)
    if address is not None:
        return address.is_global
    if _legacy_ipv4(host) is not None:
        return False
    if not speed_host_is_safe(host):
        return False
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return _doh_resolve_global(host)
    addresses = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0]))
        except (IndexError, ValueError):
            return False
    if not addresses:
        return _doh_resolve_global(host)
    if all(address in FAKE_IP_NETWORK for address in addresses):
        return _doh_resolve_global(host)
    return all(address.is_global for address in addresses)


def build_selector() -> ProxySpeedSelector:
    controller = controller_url(
        os.getenv("MIHOMO_SPEED_CONTROLLER", DEFAULT_CONTROLLER).strip() or DEFAULT_CONTROLLER
    )
    speed_url = valid_http_url(
        os.getenv("MIHOMO_SPEED_URL", DEFAULT_SPEED_URL).strip() or DEFAULT_SPEED_URL,
        "MIHOMO_SPEED_URL",
    )
    speed_host = urlparse(speed_url).hostname
    canonical_speed_host = canonical_hostname(speed_host or "")
    if not speed_host_is_safe(speed_host or "") or not speed_host_resolves_global(speed_host or ""):
        raise SpeedSelectorError("MIHOMO_SPEED_URL 不得指向本机、内网或本地域名，且必须解析到公网地址")
    if canonical_speed_host in {
        "www.lovelive-anime.jp",
        "lovelive-anime.jp",
        "lovelive-as.bushimo.jp",
        "img.sunrise-inc.co.jp",
        "img.sunrise-inc.jp",
    }:
        raise SpeedSelectorError("MIHOMO_SPEED_URL 不得指向官网或官网图片主机，测速任务不会访问官网")
    return ProxySpeedSelector(
        config_path=Path(os.getenv("MIHOMO_SPEED_CONFIG", "/config/config.yaml")),
        state_path=Path(os.getenv("MIHOMO_SPEED_STATE", "/state/proxy-speed.json")),
        nodes_path=Path(os.getenv("MIHOMO_SPEED_NODES", "/state/nodes.json")),
        controller=controller,
        group=os.getenv("MIHOMO_SPEED_GROUP", DEFAULT_GROUP).strip() or DEFAULT_GROUP,
        proxy_host=os.getenv("MIHOMO_SPEED_PROXY_HOST", DEFAULT_PROXY_HOST).strip() or DEFAULT_PROXY_HOST,
        speed_url=speed_url,
        candidate_count=env_int("MIHOMO_SPEED_CANDIDATES", DEFAULT_CANDIDATE_COUNT, 1, 32),
        bytes_to_read=env_int("MIHOMO_SPEED_BYTES", DEFAULT_BYTES, 64 * 1024, 8 * 1024 * 1024),
        timeout_seconds=env_float("MIHOMO_SPEED_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 5, 300),
        min_gain=env_float("MIHOMO_SPEED_MIN_GAIN", DEFAULT_MIN_GAIN, 0, 10),
        start_delay_seconds=env_float("MIHOMO_SPEED_START_DELAY_SECONDS", 0, 0, 3600),
        controller_secret=_required_controller_secret(),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        selector = build_selector()
        return selector.run_once()
    except (SpeedSelectorError, OSError) as exc:
        log(f"配置或测速失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
