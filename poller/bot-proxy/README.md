# bot 专用代理（独立 mihomo + 固定入口兜底）

给 bot 一个**独立于轮询、也独立于路由器**的出口：国内地址直连，其余走代理；节点故障自动切换，
代理进程不可用时由固定入口切到备用上游。

```text
bot ──HTTP(S)_PROXY──▶ bot-gateway ──▶ bot-mihomo（fallback 组 + GEOIP 国内直连）
  http://bot-gateway:7890   │              └─ 全是可用节点池里的节点
                            └─备用──▶ 路由器 Clash（默认 192.168.10.1:7890）
```

## 1. 生成初始配置

推荐**只放健康检查确认可用的节点**（这些节点已经实测能过官网，通常也通其他外网）：

```bash
# 1) 从轮询的健康检查结果导出可用节点名单
cd ../                    # poller/
docker compose exec -T proxy-health python -c "
import json
print('\n'.join(n['node'] for n in json.load(open('/state/nodes.json'))['nodes'] if n['status']==200))
" > /tmp/healthy-nodes.txt

# 2) 用精确名单渲染 bot 配置：单端口 + fallback + 国内直连
cd -
python scripts/render_mihomo_config.py --profile /tmp/sub.yaml \
  --out poller/bot-proxy/config.yaml \
  --node-list /tmp/healthy-nodes.txt \
  --no-listeners --cn-direct --group-type fallback --exclude IPv6
```

`bot-subscription` 也会优先按 `/poller-state/nodes.json` 里的健康名单过滤（已通过外置卷只读挂载），
所以订阅更新后不会把一堆坏节点塞回来；健康名单未命中时才回退到地区关键字过滤。

配置要点（脚本已生成）：

```yaml
proxy-groups:
  - name: PROXY
    type: fallback                              # 节点挂了自动换下一个
    proxies: [ ... ]
    url: https://www.gstatic.com/generate_204   # 健康检查地址（不是官网）
    interval: 120
    lazy: true
rules:
  - GEOSITE,cn,DIRECT              # 国内直连：按域名判断
  - GEOIP,CN,DIRECT,no-resolve     # 仅兜底 IP 直连，不解析域名
  - GEOIP,LAN,DIRECT,no-resolve
  - MATCH,PROXY
```

**为什么用 GEOSITE 而不是直接 GEOIP**：家用路由器常见 fake-ip DNS（返回 `198.18.x.x`），
`GEOIP` 会把国外域名解析出的 fake-ip 当成内网/保留地址而误判成直连——实测 `api.ipify.org`
就走成了直连。改成按域名匹配的 `GEOSITE` 后：国外 → 节点出口，国内 → 家宽直连。
（`GEOIP,...,no-resolve` 只对 IP 直连的请求生效，不会再误判域名请求。）

## 2. 启动

```bash
cd poller/bot-proxy
docker compose up -d --build
docker compose ps
```

订阅地址沿用 `poller/news-poller.env`（同一个 `MIHOMO_SUB_URL`）；未配置时 `bot-subscription`
会正常退出，配置只由第 1 步命令维护。`bot-subscription` 会按 **fallback + 国内直连 + 单端口**
的形状重新渲染，不会覆盖成轮询用的 per-node 端口配置。

## 3. 验证

```bash
# 国内地址：应直连（出口是家宽 IP）
docker compose exec bot-mihomo curl -sS -x http://127.0.0.1:7890 --max-time 20 https://myip.ipip.net

# 国外地址：应走节点（出口是节点 IP，与上面不同）
docker compose exec bot-mihomo curl -sS -x http://127.0.0.1:7890 --max-time 20 https://api.ipify.org; echo

# 通过固定入口（bot 实际使用的地址）
docker compose exec bot-gateway sh -c "wget -qO- --timeout=20 https://api.ipify.org" 2>/dev/null || \
  docker compose exec bot-mihomo curl -sS -o /dev/null -w 'gateway HTTP %{http_code}\n' -x http://bot-gateway:7890 https://www.google.com

# 当前出口节点与健康状态
docker compose exec bot-mihomo curl -sS http://127.0.0.1:9090/proxies/PROXY
```

## 3.1 实测结果（供排查参考）

```text
经 bot-gateway → https://api.ipify.org   → 202.85.76.136   （节点出口，已代理）
经 bot-gateway → https://myip.ipip.net   → 39.146.68.253   （中国 安徽 芜湖，家宽直连）
停掉 bot-mihomo 后同一请求              → 160.191.40.194   （自动切到备用上游，仍 HTTP 200）
恢复 bot-mihomo 后同一请求              → 202.85.76.136   （自动切回主上游）
```

## 4. bot 侧怎么接

推荐把 bot 容器接到本项目的网络（不暴露端口到局域网）：

```bash
docker network connect nijidb-bot-proxy_proxy awesome-bot
```

然后在 bot 的环境里设置（重建容器生效）：

```text
HTTP_PROXY=http://bot-gateway:7890
HTTPS_PROXY=http://bot-gateway:7890
NO_PROXY=localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,.lan,host.docker.internal,napcat,nijidb-web
```

要点：

- **`NO_PROXY` 必须包含 NapCat / OneBot 等内网地址**，否则 bot 与 NapCat 的内部通信也会进代理。
- `httpx` / `requests` 认这些环境变量；`aiohttp` 需要 `trust_env=True`；`websockets` 部分版本不读
  环境变量——只要内网地址在 `NO_PROXY` 里，websocket 直连内网就不受影响。
- 不想动 bot 的网络时，打开 compose 里的 `ports`（例如 `192.168.10.75:7899:7890`），
  再让 bot 用 `HTTP_PROXY=http://172.17.0.1:7899`（Docker 网桥网关）或 `http://192.168.10.75:7899`。
  注意那等于在局域网暴露一个无认证代理，请自行权衡。

## 5. 失效兜底

| 层 | 情形 | 兜底方式 |
| --- | --- | --- |
| 1 | 单个节点挂掉/被拒 | `type: fallback` + 健康检查，mihomo 自动切下一个节点 |
| 2 | 某地区整体不可用 | 池子里放多个地区（默认取香港/台湾/日本/新加坡/美国） |
| 3 | mihomo 进程/容器挂掉 | `restart: unless-stopped` + healthcheck，Docker 秒级重启 |
| 4 | 整个主上游不可用 | `bot-gateway`（nginx stream）自动切备用上游（默认路由器 Clash） |

要换备用上游就改 `nginx.conf` 里的 `server ... backup` 一行，然后
`docker compose restart bot-gateway`。

**关于「干脆直连」**：环境变量形式的代理无法在失败时自动降级为直连——那需要在 bot 的 HTTP 客户端
里做（例如 httpx 传 `proxy=`，异常时用 `proxy=None` 重试）。若 bot 是自己写的，建议加上这一层；
否则第 1–4 层已覆盖现实中的失败模式。

## 注意事项

- `poller/bot-proxy/config.yaml` 含节点凭据，已在 `.gitignore` 中；不要提交、不要贴进日志。
- 这个出口只给 bot 用，与轮询的出口互不影响；两边可以独立换节点、独立重启。
- `bot-gateway` 只做 TCP 透传，不会解析或改写流量；国内直连等策略由 `bot-mihomo` 决定。
