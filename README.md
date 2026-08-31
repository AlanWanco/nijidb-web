# nijidb-web
虹咲官方音乐商品的本地 Web UI 与更新通知服务。

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

打开 `http://localhost:8000`。设置页默认每 10 分钟检查一次目录、每 5 分钟检查一次异步详情，两个间隔都可以分别调整。OneBot 的地址、Token 和目标写入管理员设置页。目标支持 `private:QQ号` 或群号。首次抓取仅初始化数据库，之后新增或发生变化的条目才会通知。

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
