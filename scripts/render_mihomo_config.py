#!/usr/bin/env python3
"""从 Clash/FlClash 订阅配置渲染最小 mihomo 配置（命令行）。

示例：
    # 先看会选中哪些节点（不打印凭据；不传 --include 即全部）
    python scripts/render_mihomo_config.py --profile profile.yaml --list

    # 生成配置：每个节点一个入口端口（7901 起）
    python scripts/render_mihomo_config.py --profile profile.yaml --out config.yaml \\
        --include Japan --exclude IPv6 --exclude '将在'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mihomo_config import (  # noqa: E402
    DEFAULT_CONTROLLER,
    GROUP_TYPES,
    DEFAULT_DNS,
    DEFAULT_GROUP,
    DEFAULT_LISTENER_PORT_BASE,
    DEFAULT_PORT,
    ConfigError,
    load_proxies,
    read_node_list,
    render,
    select_exact,
    select_nodes,
    write_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染只含指定节点的 mihomo 配置")
    parser.add_argument("--profile", required=True, help="Clash/FlClash 订阅配置文件")
    parser.add_argument("--out", help="输出路径；只配合 --list 时可省略")
    parser.add_argument("--include", action="append", default=[], help="只保留名称包含这些关键字的节点（可重复；默认全部）")
    parser.add_argument("--exclude", action="append", default=[], help="排除包含这些关键字的节点（可重复）")
    parser.add_argument("--group-name", default=DEFAULT_GROUP, help=f"兜底选择组名称，默认 {DEFAULT_GROUP}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"兜底 mixed-port，默认 {DEFAULT_PORT}")
    parser.add_argument("--listener-port-base", type=int, default=DEFAULT_LISTENER_PORT_BASE, help="per-node 入口起始端口")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER, help="external-controller")
    parser.add_argument("--dns", default=",".join(DEFAULT_DNS), help="mihomo 自身解析用的 DNS，逗号分隔")
    parser.add_argument(
        "--group-type",
        choices=GROUP_TYPES,
        default="select",
        help="出口组类型：select（默认）/ fallback（节点故障自动切换）/ url-test",
    )
    parser.add_argument("--cn-direct", action="store_true", help="国内与内网地址直连，其余走代理（通用客户端用）")
    parser.add_argument("--no-listeners", action="store_true", help="不生成 per-node 入口端口（只保留一个 mixed-port）")
    parser.add_argument("--node-list", help="精确节点名单文件（每行一个名称，通常来自健康检查结果）")
    parser.add_argument("--list", action="store_true", help="只列出匹配到的节点名")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # 不传 --include 时包含订阅里的全部节点（由健康检查筛出真正可用的）
    includes = list(args.include)
    try:
        proxies = load_proxies(Path(args.profile))
        if args.node_list:
            names = select_exact(proxies, read_node_list(Path(args.node_list)), args.exclude)
        else:
            names = select_nodes(proxies, includes, args.exclude)
        if not names:
            print(f"没有匹配到节点（include={includes} exclude={args.exclude}）", file=sys.stderr)
            return 1
        if args.list:
            print(f"匹配到 {len(names)} 个节点：")
            for index, name in enumerate(names):
                extra = "" if any(key.lower() in name.lower() for key in includes) else "（dialer-proxy 依赖）"
                print(f"  {args.listener_port_base + index}  {name}{extra}")
            return 0
        if not args.out:
            print("需要 --out 指定输出路径（或使用 --list）", file=sys.stderr)
            return 2
        text = render(
            names,
            proxies,
            port=args.port,
            controller=args.controller,
            dns=tuple(item.strip() for item in args.dns.split(",") if item.strip()),
            group=args.group_name,
            listener_port_base=args.listener_port_base,
            listeners_enabled=not args.no_listeners,
            group_type=args.group_type,
            cn_direct=args.cn_direct,
        )
        write_config(Path(args.out), text)
    except ConfigError as exc:
        print(f"渲染失败：{exc}", file=sys.stderr)
        return 1
    detail = (
        f"入口端口 {args.listener_port_base}–{args.listener_port_base + len(names) - 1}"
        if not args.no_listeners
        else f"单一端口 {args.port}"
    )
    print(
        f"已写入 {args.out}（权限 600，含凭据，勿提交）：{len(names)} 个节点，{detail}"
        f"，组类型 {args.group_type}" + ("，国内直连" if args.cn_direct else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
