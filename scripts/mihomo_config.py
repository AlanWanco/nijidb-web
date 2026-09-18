"""把 Clash/FlClash 订阅配置渲染成最小 mihomo 配置。

只保留指定节点，并为每个节点开一个独立入口端口（``listeners``），这样：
* 轮询可以每趟随机挑一个可用节点，互不干扰、也不需要抢占同一个选择组；
* 健康检查可以逐端口探测，不影响正在进行的轮询。

节点凭据只存在于渲染结果里，不要把结果提交到 Git。
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

import yaml

HEADER = """\
# 由 scripts/render_mihomo_config.py 生成，请勿手工编辑，也不要提交到 Git（内含节点凭据）。
# 订阅更新：docker compose 里的 subscription 服务会定期重新渲染并让 mihomo 重载。
"""

# 每个节点一个入口端口：7901 起顺延
DEFAULT_PORT = 7890
DEFAULT_CONTROLLER = "0.0.0.0:9090"
DEFAULT_DNS = ("223.5.5.5", "1.1.1.1")
DEFAULT_GROUP = "PROXY"
DEFAULT_LISTENER_PORT_BASE = 7901
# 组类型：select（固定选择）/ fallback（节点挂了自动换）/ url-test（按延迟自动选）
GROUP_TYPES = ("select", "fallback", "url-test")
DEFAULT_GROUP_TYPE = "fallback"


class ConfigError(RuntimeError):
    """订阅配置无法解析或没有可用节点。"""


HOSTNAME_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
MAX_PROXY_NAME_LENGTH = 256


def validate_controller_listen(value: Any) -> str:
    raw = str(value or "").strip()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw) or "\\" in raw:
        raise ConfigError("external-controller 地址格式无效")
    if raw.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d{1,5})", raw)
        if not match:
            raise ConfigError("external-controller 必须是 host:port 地址")
        host, port_text = f"[{match.group(1)}]", match.group(2)
    else:
        match = re.fullmatch(r"([^:]+):(\d{1,5})", raw)
        if not match:
            raise ConfigError("external-controller 必须是 host:port 地址")
        host, port_text = match.groups()
    try:
        port = int(port_text)
    except ValueError as exc:  # pragma: no cover - guarded by the regex
        raise ConfigError("external-controller 端口格式无效") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("external-controller 端口必须在 1–65535 范围内")
    validate_proxy_host(host, "external-controller 主机")
    return raw


def validate_proxy_host(value: Any, field: str = "代理主机") -> str:
    raw = str(value or "").strip()
    valid = False
    if raw.startswith("[") and raw.endswith("]"):
        try:
            valid = ipaddress.ip_address(raw[1:-1]).version == 6
        except ValueError:
            valid = False
    else:
        valid = bool(HOSTNAME_RE.fullmatch(raw))
    if (
        not raw
        or len(raw) > 253
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        or "\\" in raw
        or not valid
    ):
        raise ConfigError(f"{field} 格式无效")
    return raw


def validate_group_name(value: Any) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        or "\\" in raw
        or any(character in raw for character in ",\n\r")
    ):
        raise ConfigError("代理组名称格式无效")
    return raw


def _read_config_text(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("配置文件不是普通文件")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise OSError("配置文件过大")
        return raw.decode("utf-8")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_proxies(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load(_read_config_text(path))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ConfigError(f"无法解析订阅配置：{path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"订阅配置格式无效：{path}")
    proxies = data.get("proxies") or []
    result: dict[str, dict[str, Any]] = {}
    for item in proxies:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        name = raw_name
        if (
            len(name) > MAX_PROXY_NAME_LENGTH
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
        ):
            raise ConfigError("订阅节点名称格式无效")
        result[name] = item
    if not result:
        raise ConfigError(f"订阅配置里没有 proxies 列表：{path}")
    return result


def read_node_list(path: Path) -> list[str]:
    """读取精确节点名单（每行一个名称，``#`` 开头忽略）。"""
    try:
        lines = _read_config_text(path).splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"无法读取节点名单：{path}") from exc
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def select_nodes(
    proxies: dict[str, dict[str, Any]],
    includes: list[str],
    excludes: list[str],
) -> list[str]:
    """按关键字挑选节点，并递归带上 dialer-proxy 依赖节点。"""

    def matched(name: str) -> bool:
        if includes and not any(key.lower() in name.lower() for key in includes):
            return False
        return not any(key.lower() in name.lower() for key in excludes)

    chosen: list[str] = []
    queue = [name for name in proxies if matched(name)]
    return _with_dependencies(queue, proxies, chosen)


def select_exact(
    proxies: dict[str, dict[str, Any]],
    preferred: list[str],
    excludes: list[str],
) -> list[str]:
    """按给定的精确名称顺序挑选节点，忽略订阅里不存在的名称。"""
    def excluded(name: str) -> bool:
        return any(key.lower() in name.lower() for key in excludes)

    queue = [name for name in preferred if name in proxies and not excluded(name)]
    return _with_dependencies(queue, proxies, [])


def _with_dependencies(
    queue: list[str],
    proxies: dict[str, dict[str, Any]],
    chosen: list[str],
) -> list[str]:
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen or name not in proxies:
            continue
        seen.add(name)
        chosen.append(name)
        dependency = proxies[name].get("dialer-proxy")
        if isinstance(dependency, str) and dependency and dependency not in seen:
            queue.append(dependency)
    return chosen


def render(
    names: list[str],
    proxies: dict[str, dict[str, Any]],
    *,
    port: int = DEFAULT_PORT,
    controller: str = DEFAULT_CONTROLLER,
    dns: tuple[str, ...] = DEFAULT_DNS,
    group: str = DEFAULT_GROUP,
    listener_port_base: int = DEFAULT_LISTENER_PORT_BASE,
    listeners_enabled: bool = True,
    group_type: str = DEFAULT_GROUP_TYPE,
    cn_direct: bool = False,
) -> str:
    """生成配置文本。

    * ``listeners_enabled``：每个节点一个入口端口（轮询用），关掉则只有一个端口。
    * ``group_type``：``select`` / ``fallback``（节点故障自动切换）/ ``url-test``（按延迟自动选）。
      部署中的 worker 另用顺序吞吐测速选择共享出口。
    * ``cn_direct``：国内 IP 直连，其余走代理（给通用客户端使用，例如 bot）。
    """
    if not names:
        raise ConfigError("没有匹配到任何节点")
    if any(
        not isinstance(name, str)
        or not name
        or len(name) > MAX_PROXY_NAME_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
        for name in names
    ):
        raise ConfigError("节点名称格式无效")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("mixed-port 必须在 1–65535 范围内")
    if type(listener_port_base) is not int or not 1 <= listener_port_base <= 65535:
        raise ConfigError("per-node 入口起始端口必须在 1–65535 范围内")
    if listeners_enabled and listener_port_base + len(names) - 1 > 65535:
        raise ConfigError("per-node 入口端口超出 65535")
    if group_type not in GROUP_TYPES:
        raise ConfigError(f"组类型只支持：{', '.join(GROUP_TYPES)}")
    controller = validate_controller_listen(controller)
    group = validate_group_name(group)

    listeners = [
        {
            "name": f"node-{index}",
            "type": "mixed",
            "port": listener_port_base + index,
            "listen": "0.0.0.0",
            "proxy": name,
        }
        for index, name in enumerate(names)
    ] if listeners_enabled else []
    rules: list[str] = []
    if cn_direct:
        # 国内直连。注意：局域网 DNS 若返回 fake-ip，基于 IP 的 GEOIP 会把国外域名误判成
        # 内网地址而直连，因此域名优先用 GEOSITE 判断，GEOIP 只兜底 IP 直连且不解析域名。
        rules += [
            "GEOSITE,cn,DIRECT",
            "GEOIP,CN,DIRECT,no-resolve",
            "GEOIP,LAN,DIRECT,no-resolve",
        ]
    rules.append(f"MATCH,{group}")

    controller_secret = os.getenv("MIHOMO_CONTROLLER_SECRET", "").strip()
    if (
        not controller_secret
        or len(controller_secret.encode("utf-8")) < 32
        or len(controller_secret.encode("utf-8")) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in controller_secret)
    ):
        raise ConfigError("MIHOMO_CONTROLLER_SECRET 必须是 32–256 字节且不含控制字符")

    config: dict[str, Any] = {
        "mixed-port": port,
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "external-controller": controller,
        "profile": {"store-selected": False, "store-fake-ip": False},
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "normal",
            "nameserver": [item for item in dns if item],
        },
        "listeners": listeners,
        "proxies": [proxies[name] for name in names],
        "proxy-groups": [
            {
                "name": group,
                "type": group_type,
                "proxies": names,
                **(
                    {"url": "https://www.gstatic.com/generate_204", "interval": 120}
                    if group_type in {"fallback", "url-test"}
                    else {}
                ),
            }
        ],
        "rules": rules,
    }
    if controller_secret:
        config["secret"] = controller_secret
    if listeners:
        config["listeners"] = listeners
    return HEADER + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def read_listeners(path: Path) -> list[dict[str, Any]]:
    """读取渲染后的配置，返回 [{name, port, node}]；用于健康检查。"""
    try:
        data = yaml.safe_load(_read_config_text(path))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取渲染配置：{path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"渲染配置格式无效：{path}")
    result: list[dict[str, Any]] = []
    for item in data.get("listeners") or []:
        if not isinstance(item, dict):
            continue
        port = item.get("port")
        node = item.get("proxy")
        if (
            type(port) is int
            and 1 <= port <= 65535
            and isinstance(node, str)
            and 0 < len(node) <= MAX_PROXY_NAME_LENGTH
            and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in node)
        ):
            result.append({"name": str(item.get("name") or f"node-{port}"), "port": port, "node": node})
    if len({item["port"] for item in result}) != len(result) or len({item["node"] for item in result}) != len(result):
        raise ConfigError("渲染配置里的 per-node listeners 端口或节点重复")
    if not result:
        raise ConfigError(f"渲染配置里没有 per-node listeners：{path}")
    return result


def write_config(path: Path, text: str) -> None:
    """原子替换配置并设置 600 权限，避免 mihomo 读取到半份 YAML。"""
    if path.parent.is_symlink():
        raise ConfigError("mihomo 配置目录不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
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
            handle.write(text)
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
    os.chmod(path, 0o600)
