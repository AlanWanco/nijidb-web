# 轮询服务（Docker，2 个容器）

官网对不可用出口返回 HTTP 403，站点服务器无法自行轮询；把官网内容的检测与更新放到另一台
能直连的机器上运行。整套只有两个容器，一眼能分清：

| 容器 | 作用 |
| --- | --- |
| `nijidb-poller-mihomo` | **唯一代理实例**。① 每个节点一个入口端口（7901 起），轮询每趟挑一个用；② 一个共享端口 `7890`，按规则分流（国内直连、其余走节点），给 bot 等其它客户端复用 |
| `nijidb-poller-worker` | **全部业务**。新闻轮询常驻；节点健康检查与订阅更新定时执行（跑完即退出，不常驻占坑） |

可选第三个容器 `image-worker`（历史图片补档）默认不启动，见 `--profile worker`。

```text
官网 ──┐
       │ 7901 / 7902 / …（每节点一个端口，轮询按趟挑一个）
  ┌────┴──────────────┐
  │ mihomo（代理实例） │── 7890（共享端口：国内直连 / 其余走节点）──▶ bot 等客户端
  └────┬──────────────┘
       │ /state/nodes.json（哪些节点可用）
  ┌────┴──────────────────────────────┐
  │ worker                            │
  │  • 新闻轮询（常驻）→ R2 直连、站点 API 直连 │
  │  • 节点健康检查（每 30 分钟一次）           │
  │  • 共享代理顺序测速选路（每 30 分钟一次）     │
  │  • 订阅更新（每 12 小时一次）              │
  └───────────────────────────────────┘
```

## 目录结构

```text
poller/
├── docker-compose.yml          # 2 个服务（+ 可选 image-worker）
├── maintainer.json             # worker 里的任务定义（常驻 / 定时）
├── news-poller.env.example     # 站点/R2 密钥与节奏模板 → 复制为 news-poller.env（600）
├── mihomo/controller.env.example # 控制台密钥模板 → 复制为 controller.env（600）
├── mihomo/Dockerfile           # 代理镜像
└── news/Dockerfile             # 业务镜像（轮询 + 补档共用）

scripts/
├── local_official_news_poller.py   # 新闻轮询
├── proxy_maintainer.py             # 容器内的常驻/定时任务托管
├── proxy_node_health.py            # 节点健康检查（--once 供定时调用）
├── proxy_speed_selector.py         # 共享代理顺序测速选路（--once 供定时调用）
├── update_mihomo_subscription.py   # 订阅更新（--once 供定时调用）
├── render_mihomo_config.py         # 从订阅渲染配置
├── mihomo_config.py                # 渲染与读取逻辑
└── remote_news_image_worker.py     # 图片补档
```

## 1. 生成初始节点配置

```bash
# 先准备独立的控制台密钥文件；不要把密钥放到命令行参数或 Git 中
cp poller/mihomo/controller.env.example poller/mihomo/controller.env
chmod 600 poller/mihomo/controller.env
$EDITOR poller/mihomo/controller.env
# 仅在当前命令期间导入密钥，渲染结果会把它写入 config.yaml
set -a; . poller/mihomo/controller.env; set +a
# 不传 --include 表示订阅里的全部节点（由健康检查筛出真正可用的）
python scripts/render_mihomo_config.py --profile ~/订阅配置.yaml \
  --out poller/mihomo/config.yaml --exclude 将在
unset MIHOMO_CONTROLLER_SECRET
```

生成的配置同时具备两种能力：

- `listeners`：每个节点一个入口端口（`7901` 起），供健康检查与轮询按端口指定出口；
  这样「每趟换节点」和「探测节点」互不干扰；
- `mixed-port: 7890` + 规则 `GEOSITE,cn,DIRECT` / `GEOIP,CN,DIRECT,no-resolve` / `MATCH,PROXY`：
  给 bot 等客户端一个共享端口，国内直连、其余走节点。

**规则为什么用 GEOSITE**：家用路由器常见 fake-ip DNS 会把国外域名解析成 `198.18.x.x`，
基于 IP 的 `GEOIP` 会把它误判成内网地址而直连；按域名匹配的 `GEOSITE` 不受影响。

## 2. 准备密钥

```bash
cp poller/news-poller.env.example poller/news-poller.env
chmod 600 poller/news-poller.env
$EDITOR poller/news-poller.env             # 站点地址、API Key、R2 凭据、节奏、可选订阅地址
```

密钥也可由管理员在站点「外部 API」页面生成（明文只显示一次）。

## 3. 启动

```bash
cd poller
docker compose up -d --build
docker compose ps          # 应只有 mihomo 与 worker 两行
docker compose logs -f worker
```

## 4. 轮询每趟怎么选出出口

1. `worker` 里的定时任务每 30 分钟探测各节点端口，写 `/state/nodes.json`。worker 使用固定公网 DNS，避免宿主局域网 DNS 把测速域名映射成 `198.18.0.0/15` fake-IP，导致公网地址校验误报；
2. 新闻轮询每趟读该文件，只考虑最近一次探测为 200 且本机没有失败记录的节点，随机挑一个
   （避免与上一趟重复），用它的端口作为本趟代理；
3. 节点返回 403 或连不上时：记录到 `/state/poller-hints.json`，立即换另一个节点重试一次，
   仍失败才进入退避。

## 5. 给 bot 或其它容器复用共享端口

```bash
docker network connect nijidb-poller_proxy <目标容器>
# 然后在该容器里设置（注意 NO_PROXY 必须包含内网地址）：
#   HTTP_PROXY=http://mihomo:7890
#   HTTPS_PROXY=http://mihomo:7890
#   NO_PROXY=localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,.lan,host.docker.internal
```

共享端口走规则：国内地址直连，其余经节点。共享组使用 `fallback` 保留故障切换；worker 每 30 分钟对少量健康候选节点顺序下载固定小文件，按实际吞吐选择最快节点，并使用 15% 的切换滞后避免抖动。测速不访问官网、不参与官网轮询，也不并发下载。

## 6. 定时任务

任务定义在 `maintainer.json`：

| 任务 | 模式 | 间隔 |
| --- | --- | --- |
| `news-poller` | 常驻 | 自身每趟随机休息 30–60 分钟 |
| `node-health` | 定时（跑完退出） | 30 分钟（`NODE_HEALTH_INTERVAL_MINUTES` 可覆盖） |
| `proxy-speed` | 定时（跑完退出） | 30 分钟 |
| `subscription` | 定时（跑完退出） | 720 分钟（`MIHOMO_SUB_INTERVAL_MINUTES` 可覆盖） |

定时任务执行完会退出，只在运行的那几秒占内存；退出后由托管器记录下次运行时间。

## 7. 验证

```bash
docker compose exec worker cat /state/nodes.json            # 可用节点
docker compose exec mihomo sh -c 'curl -fsS -H "Authorization: Bearer $MIHOMO_CONTROLLER_SECRET" http://127.0.0.1:9090/version'

# 共享端口：国内应显示家宽出口，国外应显示节点出口
docker compose exec worker sh -c 'curl -sS -x http://mihomo:7890 https://myip.ipip.net'
docker compose exec worker sh -c 'curl -sS -x http://mihomo:7890 https://api.ipify.org'

# 轮询日志里能看到本趟选中的节点
docker compose logs --tail 20 worker | grep 本趟出口

# 只检查不写入
docker compose exec worker python scripts/local_official_news_poller.py --dry-run --once
```

## 8. 环境变量速查

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NIJIDB_BASE_URL` / `NIJIDB_INGEST_API_KEY` | 必填 | 站点地址与新闻资源密钥 |
| `MIHOMO_CONTROLLER_SECRET` | 必填 | mihomo 控制台 Bearer 密钥，单独保存在 `poller/mihomo/controller.env` |
| `R2_*` | 必填 | R2 凭据与公开地址，`R2_IMAGE_PREFIX` 默认 `images` |
| `NEWS_POLL_MAX_PAGES` | 1 | 每趟扫描官网列表页数 |
| `NEWS_POLL_REST_MIN_MINUTES` / `NEWS_POLL_REST_MAX_MINUTES` | 30 / 60 | 趟间隔随机区间 |
| `NEWS_POLL_ARTICLE_DELAY_SECONDS` / `NEWS_POLL_IMAGE_DELAY_SECONDS` | 30 / 10 | 篇间 / 图间间隔 |
| `NEWS_POLL_BACKOFF_MINUTES` | 30 | 官网 403 退避 |
| `NODE_HEALTH_INTERVAL_MINUTES` | 30 | 健康检查周期 |
| `NODE_HEALTH_CONCURRENCY` | 8 | 并发探测数 |
| `NODE_HEALTH_BAD_BATCH` | 6 | 每轮额外轮换探测的不可用节点数 |
| `MIHOMO_SPEED_CANDIDATES` | 8 | 每轮顺序测速的候选节点数 |
| `MIHOMO_SPEED_BYTES` | 524288 | 每个候选节点最多读取的测速字节数 |
| `MIHOMO_SPEED_TIMEOUT_SECONDS` | 25 | 单个候选节点的测速超时 |
| `MIHOMO_SPEED_MIN_GAIN` | 0.15 | 新节点至少快多少才切换（0.15 = 15%） |
| `MIHOMO_SPEED_URL` | Cloudflare 小文件 | 固定测速地址；不要填写带凭据的 URL |
| `MIHOMO_SUB_URL` | 空 | 订阅地址；留空则不做订阅更新 |
| `MIHOMO_SUB_INTERVAL_MINUTES` | 720 | 订阅检查周期 |
| `MIHOMO_SUB_UA` | `clash-verge/v1.7.7` | 拉订阅用的 UA（订阅商按 UA 返回不同格式） |
| `MIHOMO_INCLUDE` / `MIHOMO_EXCLUDE` | 空 / `将在` | 节点名关键字过滤 |
| `MIHOMO_HEALTHY_FILE` | 空 | 只保留健康名单里的节点（如 `/state/nodes.json`） |

## 注意事项

- `poller/mihomo/config.yaml`、`poller/news-poller.env` 与 `poller/mihomo/controller.env` 含凭据，已在 `.gitignore` 中；
  不要提交、不要贴进日志。控制台仅在宿主机 `127.0.0.1:9099` 发布，容器内请求必须带 Bearer 密钥。
- 轮询保持串行、低频、固定 UA，不使用 Cookie、并发或 UA 轮换。
- 官网详情页不返回 `ETag`/`Last-Modified`，每趟都会完整取一次详情；变更判断用解析后的
  内容指纹，而不是原文哈希。
- 同一站点只应有一个常驻轮询实例；多环境部署时各自使用不同的状态文件。
