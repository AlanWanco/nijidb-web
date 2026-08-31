# nijidb-web
虹咲官方音乐商品的本地 Web UI 与更新通知服务。

## 界面预览

![虹咲音乐档案详情页预览](docs/nijidb-detail.png)

## OneBot V11 提醒

设置页的“接收目标”使用以下格式：

- 私聊：`private:QQ号`
- 群组：`group:群号`

例如：`private:123456789` 或 `group:987654321`。Access Token 仍填写在单独的 Token 输入框中。

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

打开 `http://localhost:8000`。设置页默认每 10 分钟检查一次目录、每 5 分钟检查一次异步详情，两个间隔都可以分别调整。OneBot 的地址、Token 和目标写入管理员设置页。首次抓取仅初始化数据库，之后新增或发生变化的条目才会通知。

`ADMIN_PASSWORD` 只在数据卷首次初始化时作为初始密码使用，数据库中保存的是 PBKDF2-SHA256 哈希。登录设置页后可以通过“修改管理员密码”更新，之后密码以 `/data/nijidb.sqlite3` 中的值为准；迁移到新宿主机时请一并保留 `nijidb-data` 数据卷。

## 开发调试

后端和前端分开启动，Vue 页面由 Vite 提供热更新，修改前端组件或样式时不需要重启服务：

```bash
# 终端一：FastAPI，后端代码修改自动重载
uvicorn app.main:app --reload --port 8000

# 终端二：Vue + Vite，前端修改即时 HMR
cd frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。Vite 会把 `/api` 和 `/media` 请求代理到 `http://127.0.0.1:8000`；如果后端使用其他地址，可设置 `VITE_BACKEND_URL`。生产 Docker 镜像会自动构建 `frontend/dist`，无需手动执行前端构建。

## TODO / Roadmap

以下事项按优先级整理，新增功能应继续保持现有的本地 SQLite、官方来源和增量同步方式。

### P0：数据库查询页

- [ ] 新增独立的 `/query` 查询页，支持按标题、艺术家、发行日期和发行 ID 搜索。
- [ ] 增加分页、结果数量、排序方式和清空筛选条件，避免一次加载全部记录。
- [ ] 后端新增查询 API，统一处理关键词转义、空结果和最大返回数量。
- [ ] 为 `title`、`artist`、`release_date` 和 `position` 增加合适的 SQLite 索引，并保留当前详情页跳转。
- [ ] 查询结果显示封面、标题、艺术家、发行日期和数据更新时间。

### P1：虹咲其他官方资料页面

- [ ] 盘点官方站点中可稳定抓取的其他资料入口，记录页面 URL、字段、更新频率和访问限制。
- [ ] 扩展独立的数据采集任务，优先覆盖成员/角色、音乐作品、演唱会/活动和新闻等页面。
- [ ] 为每类资料保留原始来源 URL、抓取时间、原始 HTML 或 JSON 快照和解析错误信息。
- [ ] 在前端增加对应的分类导航、列表页和详情页，避免所有内容继续挤在 CD 目录中。
- [ ] 新资料类型首次导入只建立底稿，不发送历史数据通知；后续变更再进入现有通知流程。

### P1：艺术家内容细化

- [ ] 将艺术家从发行记录中的纯文本字段抽取为可复用实体，支持别名、日文名、英文名和官方链接。
- [ ] 建立艺术家详情页，展示参与的发行、曲目、作词/作曲/编曲等 credit，以及相关成员或组合。
- [ ] 支持从艺术家页反查作品，也支持从发行详情页跳转到艺术家页。
- [ ] 统一组合、成员、声优和制作人员的分类规则，处理同名、别名和多艺术家协作。
- [ ] 查询页增加艺术家筛选和按艺术家聚合统计。

### P2：数据模型与体验完善

- [ ] 评估 `artists`、`artist_aliases`、`release_artists` 和 `track_artists` 等关系表，避免继续依赖逗号分隔文本。
- [ ] 为新增表设计可重复执行的迁移流程，不破坏现有 `releases` 数据和 `/data` 数据卷。
- [ ] 为每条记录显示“官方发布时间”和“本地资料更新时间”，明确区分官网日期与 UTC 存储时间的本地化展示。
- [ ] 增加查询、解析、数据迁移和官方页面结构变化的回归测试。

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
