# nijidb-web
虹咲官方音乐商品与节目档案的本地 Web UI，以及更新通知服务。

## 界面预览

![虹咲音乐档案详情页预览](docs/nijidb-detail.png)

## 生产启动

```bash
docker build -t nijidb-web .
docker run -d --name nijidb-web -p 8000:8000 \
  -v nijidb-data:/data \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD='change-this-password' \
  -e ADMIN_SECRET='change-this-secret' \
  nijidb-web
```

打开 `http://localhost:8000`。设置页默认开启音乐自动检查，每 10 分钟检查一次目录、每 5 分钟检查一次异步详情，三个轮询间隔都可分别调整到最多 5 小时；关闭自动检查后仍可手动执行“立即检查”。OneBot 的地址、Token 和目标写入管理员设置页。首次抓取仅初始化数据库，之后新增或发生变化的条目才会通知；官网访问进入故障或从故障恢复时，也只分别发送一次状态通知。

`ADMIN_PASSWORD` 只在数据卷首次初始化时作为初始密码使用，数据库中保存的是 PBKDF2-SHA256 哈希。首次初始化还会创建 `editor` 编辑者账号，初始密码与管理员密码相同；管理员之后修改密码不会改变编辑者密码。登录设置页后可以通过“修改管理员密码”更新，也可以在账号安全页单独更新编辑者密码（旧编辑者会话会立即失效），之后密码以 `/data/nijidb.sqlite3` 中的值为准；迁移到新宿主机时请一并保留 `nijidb-data` 数据卷。

## R2 图片

运行时封面、节目返图、新闻图片和联动立绘保存在 `/data/images`。管理员新上传的节目返图、新闻图片和联动立绘会在运行时直接上传到 Cloudflare R2；本地文件只作为备份。官网新闻刷新服务会先把新新闻图片归档到 R2，再提交新闻；`scripts/remote_news_image_worker.py` 负责历史新闻图片补档。R2 S3 Endpoint 仅用于服务端上传；要让浏览器读取图片，还需要在 `R2_PUBLIC_BASE_URL` 填写 R2 自定义域名或 `r2.dev` 公共地址。

正式迁移前可运行 `uv run --locked python scripts/prepare_production_database.py --database /data/nijidb.sqlite3` 检查本地路径；该脚本只清除 SQLite 本地路径、不删除本地备份文件，并会保留在线数据库备份。

## 外部内容导入 API

管理员设置页的“外部 API”分页提供四类接口的字段说明、JSON 示例和密钥生成入口。接口统一使用 `X-Nijidb-API-Key` 请求头，但音乐、节目、联动立绘和新闻分别使用独立密钥：

- `POST /api/ingest/music`：`NIJIDB_MUSIC_INGEST_API_KEY`
- `POST /api/ingest/program`：`NIJIDB_PROGRAM_INGEST_API_KEY`
- `POST /api/ingest/collabo`：`NIJIDB_COLLABO_INGEST_API_KEY`
- `POST /api/ingest/news`：`NIJIDB_INGEST_API_KEY`

管理员可在外部 API 页面为资源生成或轮换密钥：`POST /api/admin/external-api-keys/{music|program|collabo|news}`。生成响应只显示一次明文，数据卷中只保存哈希；镜像重建不会丢失，轮换会立即使旧密钥失效。没有管理员生成的数据库密钥时，接口才回退到对应环境变量。节目 API 接收完整主节目组 JSON，按 `program.id` 进行幂等新增或更新；联动 API 可使用稳定的 `source_id`，新闻 API 的图片需使用来源 URL，并只接受当前实例 R2 公开地址作为 `public_url`。所有接口都拒绝数据库文件、数据库路径和本地图片路径，单次 JSON 请求上限为 8 MB。

外部更新器应把图片先上传到 R2，再提交对应资源；未归档的图片可以暂时保留原始 `source_url`。如果使用环境变量回退模式，部署时将四个密钥分别注入 Web 容器，例如：

```bash
-e NIJIDB_MUSIC_INGEST_API_KEY='独立的音乐 API Key' \
-e NIJIDB_PROGRAM_INGEST_API_KEY='独立的节目 API Key' \
-e NIJIDB_COLLABO_INGEST_API_KEY='独立的联动 API Key' \
-e NIJIDB_INGEST_API_KEY='现有的新闻 API Key'
```

## 异地官网轮询（官网 403 环境）

当站点服务器所在出口被官网拦截时（官网返回 HTTP 403，响应体却是官网自己的 404 页），
服务器自身无法再轮询官网。此时把官网内容的检测与更新放到另一台能直连官网的机器上运行：

| 脚本 | 职责 |
| --- | --- |
| `scripts/local_official_news_poller.py` | 官网新闻内容检测与更新：抓列表 → 解析详情 → 图片上传 R2 → `POST /api/ingest/news` |
| `scripts/remote_news_image_worker.py` | 历史新闻图片的批量归档（补档），按站点新闻索引顺序处理 |

容器化部署（独立出口代理 + 轮询服务）见 [`poller/README.md`](poller/README.md)，支持以后追加音乐等其它轮询服务。

### 轮询行为

- 默认只扫描官网新闻列表第一页（用 `NEWS_POLL_MAX_PAGES` 可调整）。
- 站点没有该文章 → 归档图片到 R2 后提交新增。
- 站点已有、官网内容变化 → 重新归档图片并提交更新。
- 站点已有、内容未变 → 跳过，不写入。
- 站点已有但图片尚未归档（封面不是当前实例的 R2 地址、站点图片数量不足，或上次仅部分图片成功）→ 补归档后提交。
- 官网返回 403 → 退避（默认 30 分钟），并可通过控制接口自动切换出口节点。
- 两趟之间随机等待 30–60 分钟，避免固定节奏。

### 变更判断

官网详情页不返回 `ETag`/`Last-Modified`，且 HTML 含时间相关动态内容（同一篇每次抓取的
原文哈希都不同）。因此变更判断使用**解析后的内容指纹**（标题、日期、分类、摘要、正文、
图片来源）；提交给接口的 `source_hash` 只作信息记录。

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `NIJIDB_BASE_URL` | 站点地址，必填，无默认值 |
| `NIJIDB_INGEST_API_KEY` | 新闻资源密钥，必填；已生成数据库密钥时用数据库密钥，否则回退到该环境变量 |
| `NEWS_POLL_PROXY` | 访问官网使用的**单条固定代理**（容器部署使用 `http://mihomo:7890`）；生产轮询配置必填，不做轮换 |
| `R2_ENDPOINT` / `R2_BUCKET` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_PUBLIC_BASE_URL` | R2 凭据与公开地址 |
| `R2_IMAGE_PREFIX` | 默认 `images`；图片写入 `<prefix>/news-remote/<sha256>.<ext>` |
| `NEWS_POLL_STATE_FILE` | 状态文件（基线、计数），默认 `~/.local/state/nijidb-news-poller/state.json` |
| `NEWS_POLL_MAX_PAGES` | 每趟扫描的官网列表页数，默认 1 |
| `NEWS_POLL_ARTICLE_DELAY_SECONDS` / `NEWS_POLL_IMAGE_DELAY_SECONDS` | 篇间/图间间隔，默认 30 / 10 |
| `NEWS_POLL_REST_MIN_MINUTES` / `NEWS_POLL_REST_MAX_MINUTES` | 两趟之间的随机间隔区间，默认 30 / 60 |
| `NEWS_POLL_BACKOFF_MINUTES` | 官网 403 后的退避时长，默认 30 |
| `NEWS_POLL_NODES_FILE` / `NEWS_POLL_HINTS_FILE` | 可选：健康节点状态与本轮失败提示文件；配置后每趟从最近探测成功的 per-node 入口随机选一个 |

站点密钥与 R2 凭据只放在运行机器上权限为 `600` 的环境文件里，不要写进仓库、状态文件或日志；
状态文件只保存内容指纹与计数。密钥通过 HTTPS 请求头发送，不要放进 URL。

### 运行

```bash
# 只检查，不下载、不上传、不提交
uv run --locked python scripts/local_official_news_poller.py --dry-run --once --pages 1 --limit 3

# 跑一趟就退出
uv run --locked python scripts/local_official_news_poller.py --once

# 常驻：每趟之间随机休息 30–60 分钟
uv run --locked python scripts/local_official_news_poller.py
```

其他参数：`--only PAGE_NAME` 只处理指定文章，`--refresh-existing` 忽略跳过条件重新提交
（对账用），`--limit N` 限制每趟检查篇数。

官网请求保持串行、低频、固定 UA，不使用 Cookie、UA 轮换或并发抓取；代理只允许配置
**单条固定出口**，不做轮换。

## 开发调试

后端和前端分开启动，Vue 页面由 Vite 提供热更新，修改前端组件或样式时不需要重启服务：

```bash
# 安装锁定的 Python 依赖
uv sync --locked

# 终端一：FastAPI，后端代码修改自动重载
uv run --locked uvicorn app.main:app --reload --port 8000

# 终端二：Vue + Vite，前端修改即时 HMR
cd frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。Vite 会把 `/api` 和 `/media` 请求代理到 `http://127.0.0.1:8000`；如果后端使用其他地址，可设置 `VITE_BACKEND_URL`。生产 Docker 镜像会自动构建 `frontend/dist`，无需手动执行前端构建。

## 联动立绘档案

`/collabo` 使用 SQLite 中的联动记录和图片元数据，管理员可在 `/admin/collabo` 编辑资料、角色标签、多个带标题/描述的开始—结束日期时间段、排序图片、设置封面、标记整条记录状态和上传本地图片。公开页支持按角色标签筛选，“全员”表示选中全部角色。应用启动时会从本地 `frontend/src/content/collaborationIllustrations.json` 与 `data/images/illustrations/manifest.json` 导入尚未入库的记录；容器部署可使用 `scripts/seed_collabo_database.py` 对数据卷执行同样的导入。原有 `/illustrations` 和 `/api/collaboration-illustrations` 保留兼容。图片文件仍保存在 Git 忽略的 `data/` / `/data` 下，不写入 SQLite；若需要在容器中启用本地图片，将该目录复制到数据卷的 `/data/images/illustrations/`，然后重启应用即可。采集脚本包括：

```bash
uv run --locked python scripts/import_collaboration_illustrations.py
uv run --locked python scripts/collect_collaboration_illustrations.py
uv run --locked python scripts/collect_wayback_illustrations.py
uv run --locked python scripts/collect_pdf_illustrations.py
uv run --locked python scripts/collect_local_illustrations.py --source-dir /Volumes/SSK/Download/bangumi-parser/ll-offical-site
```

联网采集脚本只接受官方页面、官方 PDF、Wayback 的官方页面快照和官方账号的原图候选，并会过滤 logo、导航、二维码、头像和站点装饰图。`collect_local_illustrations.py` 不联网，只按本地 Markdown 的页面 ID 和图片序号补入资源，并按 SHA-256 去重且不覆盖已有文件。清单中的 `complete` 表示已收录 3 张，`partial` 表示目前只有 1–2 张，`unavailable` 表示暂未找到可验证的本地资源，需继续补充来源。初次部署时可执行：

```bash
uv run --locked python scripts/seed_collabo_database.py \
  --database /data/nijidb.sqlite3 \
  --index /data/images/illustrations/collaborationIllustrations.json \
  --manifest /data/images/illustrations/manifest.json
```

## 官网新闻

`/news` 使用独立的 `news_articles`、`news_images`、`news_fetch_state` 和 `news_sync_log` 表。Topics 与 Nijigasaki News 在展示/筛选中合并为 **Official Site News**（`source=official_site`），AS News 单独展示；原始来源、新闻 ID 和链接不变，旧 source 参数仍可使用。可以把本地 `ll-offical-site/*.md` 一次导入数据库；导入器默认只保存 `pic/` 相对路径，不会复制约 6GB 的原始图库：

```bash
uv run --locked python scripts/import_official_news.py \
  --root /Volumes/SSK/Download/bangumi-parser/ll-offical-site \
  --database data/nijidb.sqlite3
```

运行本地后端时设置 `NEWS_ARCHIVE_DIR` 指向该归档目录，页面会通过新闻图片接口读取本地图片。需要将图片复制到数据卷时再加 `--copy-images`。

- 搜索支持标题、摘要、正文、分类、来源页名和标签；联动搜索支持标题、备注、来源、图片说明、角色标签和时间段标题/描述等元数据。正文支持 GFM Markdown（标题、列表、表格、引用、代码等），HTML 经 DOMPurify 清理；会过滤官网分类导航和当前分类标签；正文与图库图片可点击放大，支持灯箱内翻图。
- 官网图片仅保留宽度至少 240 px、高度至少 120 px 且不超过 10000 px 的资源；页面未声明尺寸时读取官方图片头部判断，图标、追踪图和异常尺寸图片不会进入图库。
- 标签保持原始 token 入库，展示按中文/日文/英文翻译；新闻支持左右键和横向滑动，保留来源、标签、搜索与页码。编辑中/灯箱内不会误切新闻。
- 自动轮询只访问 `https://www.lovelive-anime.jp/nijigasaki/topics.php` 最近四页及详情，检查正文变化；通过 ETag / Last-Modified 减少重复传输，429/临时错误有界重试。设置开关或间隔变更立即唤醒调度器，音乐目录、音乐详情和 Topics 间隔上限均为 300 分钟（5 小时）。
- 管理员设置页的“手动源代码”支持音乐目录和新闻详情：优先由当前浏览器直接请求官网，再把 HTML 送到 `POST /api/admin/source-html` 解析；服务器不做代理，也不接受任意外部 URL。浏览器受 CORS 限制时可选择保存的 HTML 文件或粘贴源代码，解析失败会保留旧资料。
- `POST /api/admin/news/{id}/refresh` 手动刷新对应官网页面，支持历史来源。服务端检查管理员权限、官网 HTTPS 白名单、重定向与响应大小；失败保留旧内容，并返回 HTTP 状态、超时、网络或解析原因。
- 编辑模式可以删除手动、归档和官网图片；来源图片删除会写入抑制记录，后续自动刷新不会悄悄恢复。图片按来源标识更新而非删除重建，保留图片 ID；正文或图片无变化不刷新 `updated_at`。保存新闻可传 `updated_at` 检测并发冲突，返回 409 时重新加载。
- `/admin?section=music|news|source|bot|api|database|account` 分区设置；桌面左侧目录、手机顶部页签。外部 API 页按资源分页显示字段和示例，已有密钥不会回显；新生成或轮换的明文只显示一次。数据库页显示音乐/节目/新闻/联动最近 200 条变化记录，支持分类筛选和每页 15 条分页。`editor` 账号只能进入节目管理、联动管理和数据库下载；设置页其他分页、数据库上传与覆盖均不可用。
- 新闻设置提供可选的慢速官方图床刷新队列：开启后按 5–60 秒间隔逐篇重新读取历史新闻，成功解析到官网图片后移除该篇归档图片引用但不删除本地备份文件；403、429、验证页等风控失败会记录页面并使用退避重试，也可以手动重新排队失败页面。默认关闭，避免新部署未经确认就请求官网。
- 新闻摘要统一限制为 200 字；应用启动和后续导入/编辑时会自动截断超出的旧值。

离线验证（临时数据库、模拟网络，不修改现有资料）：

```bash
uv run --locked python -m unittest discover -s tests -v
PLAYWRIGHT_MODULE=/path/to/playwright node frontend/tests/news-browser.cjs
PLAYWRIGHT_MODULE=/path/to/playwright node frontend/tests/program-period-browser.cjs
```

浏览器测试自动启动临时 Vite，并使用无头 Chrome 验证 Markdown、灯箱、新闻切换、设置分页、节目时期自动生成开关及移动端布局。

## 节目档案

打开 `/programs` 查看节目播出日历；登录后打开 `/admin/programs` 手动维护节目资料和排期。排期支持周更、月更、逐期设置、单次和多个分段时期。月更明确分为有规律和无规律：有规律可按周次计算（顺数/倒数第几周和星期）或按日计算（顺数 1–28 日、倒数 1–14 日）；无规律日期不定但可以设置默认播出时间，新建节目首期使用时期开始日期，开启后续自动生成时从下个月 1 日开始按月生成占位单集。逐期设置是完全手工的独立选项，不代表月更；仅新建节目时会按时期开始日期和默认播出时间先创建首期，之后可以设置默认时间作为新增单集的初始值，也可以按实际日期跨数月录入并逐期修改时间。单次不显示时期级开关，新建时按播出日期和时间创建一条单集。读取、修改已有节目、切换自动生成开关和覆盖导入均不补建首期；导入已整理的单集时也不追加首期。只有一个周更或月更时期时，时期级开关与节目级开关保持同步；多个时期时，节目级开关仍是总开关，各时期可以分别关闭自动生成。对异常节目的单集列表也可以关闭节目级自动生成，改用“添加单集”逐条录入，或将固定月更批量切换为逐期设置。每期可以在备注后上传节目返图（配置 R2 时同步到 R2）或保存图片直链，日历侧栏、节目详情和列表均显示缩略图并支持点击查看大图。一个主节目下可以挂载多个独立配置的子节目，主节目 key 固定为“主节目”。开启自动生成的进行中节目会按规则生成未来约半年的单集，也可以对单集进行改期、取消或补录。“未更新”仅按当前排期规则推算，不代表真实播出状态。

节目编辑页支持单节目 JSON 导入和导出。导出会根据当前节目设置生成唯一格式：保留 `auto_generate`、`periods`（包括每个时期的 `auto_generate`）和当前生效的 `occurrences`；开启自动生成的节目会保留规则并导出当前生成窗口，关闭自动生成的节目则按当前单集作为最终数据导出。导出的 `import_options.schedule_mode` 为 `current`，导入时按每个 `program` 和 `period` 的 `auto_generate` 还原设置；手动编写 JSON 时仍可使用 `individual` 或 `generated`。每个单集都可以填写可选的 `title` 和 `images`（HTTP/HTTPS 图片直链及 `alt_text`），有值时会显示在日历、单集详情和 JSON 中。同一节目同一天允许多个单集，但播出时间必须不同；未填写时间的单集不能与同日其他单集并列。导入会先显示节目、排期和全部单集预览，默认新建；检测到同 ID 或同名称的节目时，可以明确选择覆盖并再次确认。JSON 导入写入前、数据库还原前和每天 00:00（Asia/Tokyo）会自动将当前数据库备份到数据卷的 `/data/backups`，并最多保留最近 30 份；设置页可以查看、下载和还原这些备份。格式模板见 [`docs/program-json-template.json`](docs/program-json-template.json)；说明字段使用合法 JSON 的 `_field_notes` 和 `_import_notes`，也兼容 `//`、`/* */` 注释。

## TODO / Roadmap

- [ ] 数据库查询页：按标题、艺术家、日期和发行 ID 搜索。
- [x] 官网新闻页：本地 Markdown 归档、Topics 自动检查、tags 筛选和页面内编辑。
- [x] 节目档案页：整理官方和个人节目资料。
- [x] 联动立绘页：整理联动视觉和相关出处。
- [ ] 艺术家详情页：关联作品、曲目和 credit。
- [ ] 跨平台账号映射和艺术家关系表。
- [ ] 补充同步、解析和数据迁移测试。

## 换宿主机

只要新宿主机可以访问 Docker Registry、npm Registry 和官方站点的 HTTPS，下面的镜像构建和运行流程不依赖当前宿主机的本地代码或缓存数据：

```bash
docker build -t nijidb-web .
docker run -d --name nijidb-web --restart unless-stopped -p 8000:8000 \
  -v nijidb-data:/data \
  -e ADMIN_USERNAME='你的管理员账号' \
  -e ADMIN_PASSWORD='首次启动密码' \
  -e ADMIN_SECRET='随机长字符串' \
  nijidb-web
```

容器启动后会立即检查 `cd.php`；之后异步检查 `cd_detail.php`，封面会下载到 `/data/images`，页面由镜像内的 Vue `dist` 提供。新宿主机使用全新的 `nijidb-data` 时会重新建立数据库并抓取资料；迁移旧卷时会保留已有资料和已经修改过的管理员密码。抓取能否成功取决于新宿主机的 DNS、HTTPS 出站网络和目标站点对该出口 IP 的访问限制。

如果 OneBot 跑在宿主机而不是另一个容器内，OneBot 地址不要填写容器内的 `127.0.0.1`：Docker Desktop 通常使用 `http://host.docker.internal:端口`，Linux 则使用宿主机网关地址或把两个容器加入同一个 Docker network。

---

## 支持作者

如果这个项目对你有帮助，欢迎请我喝杯咖啡 ☕️

<p align="center">
  <img src="./docs/buy-me-a-coffee.png" alt="Buy me a coffee" width="580">
</p>
