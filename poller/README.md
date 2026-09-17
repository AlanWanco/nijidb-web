# 轮询服务（Docker）

本文件夹集中存放「异地轮询」相关的容器化服务：独立出口代理、节点健康检查、订阅自动更新、
内容轮询。站点服务器出口被官网拦截时（官网返回 HTTP 403），把这些服务部署到另一台机器上，
让**官网请求**走可用的固定出口，站点 API 与 R2 仍然直连。

## 目录结构

```text
poller/
├── docker-compose.yml          # mihomo + subscription + proxy-health + news-poller（+ 可选 image-worker）
├── news-poller.env.example     # 密钥与节奏模板 → 复制为 news-poller.env（600）
├── mihomo/                     # 出口代理镜像（配置在运行时只读挂载）
│   └── Dockerfile
└── news/                       # 新闻轮询镜像（内容轮询与补档共用）
    └── Dockerfile

scripts/                        # 容器内实际执行的脚本
├── render_mihomo_config.py     # 从订阅配置渲染节点配置（每个节点一个入口端口）
├── mihomo_config.py            # 渲染与读取逻辑
├── update_mihomo_subscription.py   # 订阅自动更新 + 让 mihomo 重载
├── proxy_node_health.py        # 逐节点探测官网，产出可用节点列表
├── local_official_news_poller.py   # 内容轮询
└── remote_news_image_worker.py     # 图片归档（补档）
```

以后要加音乐轮询：新建 `music/`（沿用 `news/` 的镜像模式），在 `docker-compose.yml` 里加一个
service，共用同一个 `mihomo` 出口；不同轮询服务使用各自的 `*_STATE_FILE`。

## 服务

| 服务 | 作用 |
| --- | --- |
| `mihomo` | 独立出口代理。配置里为**每个节点开一个入口端口**（7901 起），不做节点轮换、不做热更新 |
| `proxy-health` | 每 `NODE_HEALTH_INTERVAL_MINUTES`（默认 30）分钟逐端口并发请求官网，写出「最近一次为 200」的可用节点列表 |
| `subscription` | 每 `MIHOMO_SUB_INTERVAL_MINUTES`（默认 720）分钟重新拉取订阅、渲染配置并让 mihomo 重载；未配置订阅地址时自行退出 |
| `news-poller` | 内容轮询：**每趟从可用节点里随机挑一个**，抓官网列表 → 解析详情 → 图片上传 R2 → 提交站点 API |
| `image-worker` | 可选，历史新闻图片批量归档（补档），`--profile worker` 启用 |

代理端口不发布到宿主，只在 compose 网络内可见；只有 `127.0.0.1:9099` 暴露 mihomo 控制台。

## 1. 生成初始节点配置

```bash
# 先看会选中哪些节点（只打印名称与端口，不输出凭据）
python scripts/render_mihomo_config.py --profile ~/订阅配置.yaml --list --include Japan

# 生成配置（自动 chmod 600，写入 poller/mihomo/config.yaml）
python scripts/render_mihomo_config.py --profile ~/订阅配置.yaml \
  --out poller/mihomo/config.yaml --include Japan --exclude IPv6 --exclude '将在'
```

- 不加 `--include` 时包含订阅里的**全部节点**（不限地区）；每个节点分配到 `7901`、`7902`…
  一个独立入口端口，健康检查与轮询都按端口访问。
- 节点质量差异很大：同一地区不同出口可能一个 200、一个 403、一个连不上。**不需要手工挑**，
  健康检查会自动把不返回 200 的节点排除在可用列表之外。实测某次订阅：128 个节点里 78 个可用
  （香港 29、台湾 15、美国 13、日本 6、其它 15），所以不必担心「日本节点全被 403」。
- 订阅地址不是必须的：填了 `MIHOMO_SUB_URL` 才会启用自动更新；否则配置只由上面的命令维护。

## 2. 准备密钥

```bash
cp poller/news-poller.env.example poller/news-poller.env
chmod 600 poller/news-poller.env
$EDITOR poller/news-poller.env    # 站点地址、API Key、R2 凭据、节奏、可选订阅地址
```

密钥也可由管理员在站点「外部 API」页面生成（明文只显示一次）；已生成数据库密钥时接口优先
使用数据库密钥，否则回退到 `NIJIDB_INGEST_API_KEY`。

## 3. 启动

```bash
docker compose up -d --build
docker compose logs -f news-poller
```

## 4. 每趟如何选出口

1. `proxy-health` 并发（`NODE_HEALTH_CONCURRENCY`，默认 8）探测每个节点端口，写出 `/state/nodes.json`。
   首次运行或订阅更新导致端口↔节点映射变化时**全量探测**（128 个节点约 1 分钟）；
   之后每轮只探测「上次可用的全部节点 + 一小批轮换的不可用节点」，既省请求也能发现新可用节点。
2. `news-poller` 每趟开始时读取该文件，只考虑**状态为 200**、且本机没有近期失败记录的节点，
   随机挑一个（避免与上一趟相同），把该节点的端口作为本趟代理。
3. 某个节点返回 403 或连接失败时：记录到 `/state/poller-hints.json`，**立即换另一个节点重试一次**；
   仍失败才进入 403 退避。健康检查会在下一轮优先重测被标记的节点。

这样既满足「每次查询走不同可用代理」，也不会因为个别节点失效而卡住整个轮询。
由于可用池跨地区，即便某个地区整体被拒（例如实测 `Japan C01`/`Japan F01` 返回 403），也不影响轮询。

## 5. 验证

```bash
# 可用节点列表
docker compose exec proxy-health cat /state/nodes.json

# 逐节点实测（应输出 200；403 表示该出口被官网拒绝）
docker compose exec mihomo curl -sS -o /dev/null -w '%{http_code}\n' \
  -x http://127.0.0.1:7901 https://www.lovelive-anime.jp/

# 轮询日志里能看到本趟选中的节点
docker compose logs --tail 20 news-poller

# 只检查不写入
docker compose exec news-poller python scripts/local_official_news_poller.py --dry-run --once
```

## 6. 运行状态

```bash
docker compose logs --tail 50 news-poller          # 轮询日志
docker compose logs --tail 20 proxy-health         # 健康检查
docker compose logs --tail 20 subscription         # 订阅更新
docker compose ps -a                               # subscription 正常退出是「未启用」状态
docker compose exec news-poller cat /state/news-poller.json   # 基线、计数、退避
docker compose restart news-poller                 # 重启轮询（会立即跑一趟）
```

状态都在具名卷 `poller-state`（`nodes.json`、`poller-hints.json`、`subscription.json`、
各服务的 state 文件），重建容器不会丢。

## 7. 订阅自动更新

在 `poller/news-poller.env` 里填：

```text
MIHOMO_SUB_URL=https://<订阅地址>
MIHOMO_SUB_INTERVAL_MINUTES=720
# MIHOMO_INCLUDE=             # 留空 = 全部节点（推荐）
# MIHOMO_EXCLUDE=将在          # 排除明确失效的占位节点
# MIHOMO_SUB_UA=clash-verge/v1.7.7   # 订阅商按 UA 返回不同格式，默认用 Clash 系
```

然后 `docker compose up -d subscription`。更新流程：

1. **直连优先**拉取订阅（部分订阅商会按请求来源 IP 返回不同节点数：实测经代理只给 11 个，
   直连给 128 个），失败才走现有出口。
2. 渲染新配置；**渲染结果为空时保留旧配置**，不会把可用出口换成空配置。
3. 内容有变化才写回 `poller/mihomo/config.yaml` 并调用 mihomo 控制台重载；无变化则跳过。
4. 写入 `/state/subscription.json`，健康检查发现它变新会立即重新探测一轮（端口与节点可能已变）。

## 8. 可选：补档

```bash
docker compose --profile worker up -d image-worker
docker compose logs -f image-worker
```

与轮询共用同一份 `news-poller.env`；worker 使用 `NEWS_WORKER_PROXY` 走同一个出口，
状态文件为 `/state/image-worker.json`。

## 9. 给其它容器复用这个出口

出口默认只在 compose 网络内可见。要让别的容器也走它，把目标容器接到同一网络：

```bash
docker network connect nijidb-poller_proxy <目标容器>
# 然后在该容器里把代理地址写成 http://mihomo:7901（或其它节点端口）
```

需要注意：多个使用者共用同一端口时走的是同一条出口链路。若某个使用者（例如 bot）需要
**独立出口**、避免与轮询互相影响，再起一份 `mihomo`（独立端口、独立节点池）或另一份 compose。

## 环境变量速查

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIHOMO_SUB_URL` | 空 | 订阅地址；留空则不启用自动更新 |
| `MIHOMO_SUB_INTERVAL_MINUTES` | 720 | 订阅检查间隔 |
| `MIHOMO_SUB_UA` | `clash-verge/v1.7.7` | 拉订阅用的 UA（订阅商按 UA 返回不同格式） |
| `MIHOMO_INCLUDE` / `MIHOMO_EXCLUDE` | 空 / `将在` | 节点名关键字过滤，留空表示全部 |
| `MIHOMO_LISTENER_PORT_BASE` | 7901 | 每节点入口端口起始值 |
| `NODE_HEALTH_INTERVAL_MINUTES` | 30 | 健康检查周期 |
| `NODE_HEALTH_CONCURRENCY` | 8 | 并发探测数 |
| `NODE_HEALTH_PROBE_DELAY_SECONDS` | 0.3 | 每个探测前的间隔 |
| `NODE_HEALTH_BAD_BATCH` | 6 | 每轮额外轮换探测的不可用节点数 |
| `NEWS_POLL_NODES_FILE` | `/state/nodes.json` | 可用节点列表（compose 已设） |
| `NEWS_POLL_HINTS_FILE` | `/state/poller-hints.json` | 失败节点提示（compose 已设） |

## 注意事项

- `poller/mihomo/config.yaml` 与 `poller/news-poller.env` 含凭据，已在 `.gitignore` 中；
  不要提交、不要贴进日志或聊天。
- 轮询保持串行、低频、固定 UA，不使用 Cookie、并发或 UA 轮换。
- 同一站点只应有一个常驻轮询实例；多环境部署时各自使用不同状态文件，避免重复抓取。
- 官网详情页不返回 `ETag`/`Last-Modified`，每趟都会完整取一次详情；变更判断使用解析后的
  内容指纹，而不是原文哈希。
