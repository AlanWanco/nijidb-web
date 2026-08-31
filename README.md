# nijidb-web
虹咲官方音乐商品的本地 Web UI 与更新通知服务。

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

- [ ] 数据库查询页：按标题、艺术家、日期和发行 ID 搜索。
- [ ] 虹咲其他官方资料页：成员、音乐、活动和新闻等。
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
