"""把 Clash/FlClash 订阅配置渲染成最小 mihomo 配置。

只保留指定节点，并为每个节点开一个独立入口端口（``listeners``），这样：
* 轮询可以每趟随机挑一个可用节点，互不干扰、也不需要抢占同一个选择组；
* 健康检查可以逐端口探测，不影响正在进行的轮询。

节点凭据只存在于渲染结果里，不要把结果提交到 Git。
"""

from __future__ import annotations

import os
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
# 组类型：select（固定选择）/ fallback（节点挂了自动换）/ url-test（自动选最快）
GROUP_TYPES = ("select", "fallback", "url-test")
DEFAULT_GROUP_TYPE = "url-test"


class ConfigError(RuntimeError):
    """订阅配置无法解析或没有可用节点。"""


def load_proxies(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法解析订阅配置：{path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"订阅配置格式无效：{path}")
    proxies = data.get("proxies") or []
    result: dict[str, dict[str, Any]] = {}
    for item in proxies:
        if isinstance(item, dict) and item.get("name"):
            result[str(item["name"])] = item
    if not result:
        raise ConfigError(f"订阅配置里没有 proxies 列表：{path}")
    return result


def read_node_list(path: Path) -> list[str]:
    """读取精确节点名单（每行一个名称，``#`` 开头忽略）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
    * ``group_type``：``select`` / ``fallback``（节点故障自动切换）/ ``url-test``。
    * ``cn_direct``：国内 IP 直连，其余走代理（给通用客户端使用，例如 bot）。
    """
    if not names:
        raise ConfigError("没有匹配到任何节点")
    if group_type not in GROUP_TYPES:
        raise ConfigError(f"组类型只支持：{', '.join(GROUP_TYPES)}")

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
    if listeners:
        config["listeners"] = listeners
    return HEADER + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def read_listeners(path: Path) -> list[dict[str, Any]]:
    """读取渲染后的配置，返回 [{name, port, node}]；用于健康检查。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
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
        if isinstance(port, int) and isinstance(node, str) and node:
            result.append({"name": str(item.get("name") or f"node-{port}"), "port": port, "node": node})
    if not result:
        raise ConfigError(f"渲染配置里没有 per-node listeners：{path}")
    return result


def write_config(path: Path, text: str) -> None:
    """就地写入（保持 inode，容器重载可见）并设置 600 权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
