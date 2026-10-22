# Nijidb 新闻轮询与图片归档

本目录只负责 Nijidb 新闻业务：官网新闻轮询、图片归档到 R2，以及可选的历史图片补档。音乐目录轮询作为同一 worker 进程中的协程运行，不增加容器或 Compose 服务。代理核心、订阅、节点健康检查和测速选路由独立的 Mihomo Compose 维护；本 Compose 不构建、不启动、不重启或配置 Mihomo。

## 服务

| 服务 | 用途 | 启动方式 |
| --- | --- | --- |
| `worker` | 官网新闻轮询；串行低频抓取，图片归档后提交站点 API | 默认启动 |
| `image-worker` | 读取已有新闻并补档图片 | `--profile worker` 可选启动 |

新闻轮询使用一个固定 HTTP 代理地址。默认通过局域网访问 Mihomo：`http://192.168.10.75:7890`。这只是服务接口依赖；由哪个代理实现、怎样更新订阅和选择节点，由独立代理 Compose 负责。

```text
官网 ──固定代理──▶ http://192.168.10.75:7890
 │                         ▲
 │                         │ 独立代理服务（本 Compose 不管理）
 ├── 新闻 worker ──────────┘
 └── 图片 worker ──────────┘

新闻 worker / 图片 worker ──直连──▶ Nijidb API、R2
音乐轮询（新闻 worker 内） ──直连──▶ Nijidb API、R2
              └──官网请求共用固定代理
```

## 配置

运行时密钥保存在 `news-poller.env`，不要提交或写入日志：

```bash
cp poller/news-poller.env.example poller/news-poller.env
chmod 600 poller/news-poller.env
$EDITOR poller/news-poller.env
```

配置至少包括站点 URL、新闻资源 API Key 和 R2 访问凭据。新闻与音乐监控使用各自独立的 API Key；管理员在站点「外部 API」页面分别生成，明文只显示一次。`NIJIDB_MUSIC_INGEST_API_KEY` 属于旧版音乐导入接口，不可代替 `NIJIDB_MUSIC_POLLER_API_KEY`。

代理地址通过 Compose 项目环境变量 `POLL_PROXY_URL` 配置。默认值是当前局域网代理地址；如需覆盖，在 `poller/.env` 中设置：

```dotenv
POLL_PROXY_URL=http://192.168.10.75:7890
```

`POLL_PROXY_URL` 必须是无凭据的 HTTP/HTTPS 代理 URL。局域网代理没有客户端认证，不要将代理端口转发到公网。也可在启动 Compose 时通过环境变量覆盖。

官网请求只使用此固定代理，不从节点列表选择 per-node 端口。不要把 `NEWS_POLL_NODES_FILE`、`NEWS_POLL_HINTS_FILE` 或 Mihomo 控制器配置加回本服务。官网代理故障时由轮询现有退避逻辑处理；代理上游的 fallback 和选路属于独立代理服务。

## 启动与验证

Bot 事件接收使用共享内网。首次部署时先创建一次（已存在则跳过）：

```bash
docker network create --internal nijidb-poller-link
```

Bot 与 Poller 两侧的 Compose 都把它声明为 `external`，不会自动创建或修改它。

```bash
cd poller
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 worker

# 从 worker 容器检查固定代理出口；只检查 HTTP 状态，不输出代理响应正文
docker compose exec worker python -c 'import httpx; r=httpx.get("https://api.ipify.org", proxy="http://192.168.10.75:7890", timeout=15, trust_env=False); print("proxy_http=" + str(r.status_code))'

# 只检查官网内容，不下载、上传或提交
# 运行环境需先配置 news-poller.env
docker compose exec worker python scripts/local_official_news_poller.py --dry-run --once --pages 1 --limit 3
```

如果覆盖了 `POLL_PROXY_URL`，验证命令里的地址也需相应替换。Docker worker 不需要加入代理项目的 Compose 网络；通过树莓派 LAN 地址访问已验证可用。

## 图片补档

`image-worker` 默认不启动。确认要运行后：

```bash
docker compose --profile worker up -d --build image-worker
docker compose logs -f image-worker
```

图片 worker 只将官网图片请求经 `NEWS_WORKER_PROXY` 发出；读取新闻 API、提交导入 API 和 R2 操作仍按各自实现直连。worker 与 image-worker 共用 `poller-state` 卷，各自状态文件互不覆盖。

## 新闻轮询行为

- 默认只扫描官网新闻列表第一页，可用 `NEWS_POLL_MAX_PAGES` 调整。
- 设置 `MUSIC_POLL_ENABLED=1` 并填入独立的 `NIJIDB_MUSIC_POLLER_API_KEY` 后，同一进程开始音乐目录轮询；音乐与新闻使用 `/state` 下不同的状态文件，并共享固定官网代理、R2 配置和容器，不增加服务。
- 音乐首次运行将现有官网发行写入站点作为静默基线；后续资料变化由站点使用数据库中保存的 OneBot 配置发送含文字和图片的合并转发通知。发送失败会保留待重试记录。
- 音乐目录查询被官网 403 拒绝时按 `MUSIC_POLL_BACKOFF_MINUTES` 退避；新闻和音乐各自退避，不影响另一条轮询继续工作。
- 官网串行、低频访问，固定 UA；不使用 Cookie、UA 轮换、并发抓取或代理轮换。
- 站点 API 直连并固定 IPv4 源地址，同时允许连接级重试；容器没有 IPv6 出口，而站点域名同时返回 AAAA 记录，不固定地址族会让整趟以 `ConnectError`/`ConnectTimeout` 失败。
- 篇间默认 30 秒、图片间默认 10 秒；趟间默认随机休息 30–60 分钟。
- 官网 403 使用现有退避策略。切换上游节点或恢复代理由独立代理服务处理。
- 站点 API 与 R2 不经新闻代理，避免无谓绕路。R2 客户端也仅为自身连接固定 IPv4，并限制连接/读取超时和重试次数。
- 图片按稳定对象 key 上传；新闻导入保持幂等。

## 主要环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POLL_PROXY_URL` | `http://192.168.10.75:7890` | 新闻与图片 worker 共用的固定代理地址 |
| `NIJIDB_BASE_URL` | 必填 | 站点地址 |
| `NIJIDB_INGEST_API_KEY` | 必填 | 新闻资源写入密钥 |
| `MUSIC_POLL_ENABLED` | `0` | 启用同一 worker 进程内的音乐轮询 |
| `NIJIDB_MUSIC_POLLER_API_KEY` | 启用音乐轮询时必填 | “音乐监控”资源专用密钥，不复用旧版音乐导入 Key |
| `MUSIC_POLL_STATE_FILE` | `/state/music-poller.json` | 音乐独立断点文件；新闻与音乐不能共用同一路径 |
| `MUSIC_POLL_ARTICLE_DELAY_SECONDS` / `MUSIC_POLL_DETAIL_DELAY_SECONDS` | `5` / 图片轮询间隔 | 音乐目录发行 / 动态详情串行请求间隔 |
| `MUSIC_POLL_BACKOFF_MINUTES` | `NEWS_POLL_BACKOFF_MINUTES` | 官网音乐请求受限后的退避时间 |
| `R2_ENDPOINT` / `R2_BUCKET` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_PUBLIC_BASE_URL` | 必填 | R2 归档配置 |
| `R2_IMAGE_PREFIX` | `images` | 图片对象前缀 |
| `NEWS_POLL_MAX_PAGES` | `1` | 每趟扫描官网列表页数 |
| `NEWS_POLL_ARTICLE_DELAY_SECONDS` / `NEWS_POLL_IMAGE_DELAY_SECONDS` | `30` / `10` | 篇间 / 图片间隔 |
| `NEWS_POLL_REST_MIN_MINUTES` / `NEWS_POLL_REST_MAX_MINUTES` | `30` / `60` | 趟间随机休息范围 |
| `NEWS_POLL_BACKOFF_MINUTES` | `30` | 官网 403 后退避 |
| `NEWS_WORKER_PROXY` | Compose 使用 `POLL_PROXY_URL` | 图片 worker 的固定代理；单独运行时可选 |

新闻、音乐和图片补档状态均保存在 `poller-state` Docker volume 中，各用独立文件。更新或重建代码不要加 `-v`，不要删除该 volume。
