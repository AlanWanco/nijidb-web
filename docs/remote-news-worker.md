# 外部新闻图片归档 Worker

`scripts/remote_news_image_worker.py` 用于在独立服务器或 Termux 上低速归档新闻图片：

1. 读取 Nijidb 的公开 `/api/news` 和 `/api/news/{id}`，按网站返回顺序处理。
2. 仅下载 `remote` 图片的 `source_url`，默认只允许 Love Live 官方站及已知官方图片 CDN。
3. 每张图片校验格式和 20 MB 大小限制后，上传到 R2 的
   `R2_IMAGE_PREFIX/news-remote/`。
4. **上传成功后**，才用 `X-Nijidb-API-Key` 请求 `POST /api/ingest/news`，把文章和
   R2 地址回传网站。
5. 默认工作 60 分钟、休息 30 分钟；处理图片之间至少间隔 30 秒。状态文件支持中断后继续，
   失败的 403 会进入 6 小时退避。

Worker 不使用 Cookie、UA 轮换或并发抓取；默认直连并使用固定浏览器式 UA，运行环境无法直连
官网时可用 `NEWS_WORKER_PROXY` 指定**单条固定代理**（不做轮换，也不使用生产代理）。网站端官方刷新遇到
HTTP 403 后会进入约 6 小时退避，外部 Worker 也会对图片 403 单独退避。

## 网站 API

网站启动时从环境变量读取单独的 API Key：

```text
NIJIDB_INGEST_API_KEY=<单独生成的随机值>
```

不要复用管理员密码、管理员 Cookie 或 R2 Secret。Key 通过 HTTPS 请求头发送，不能放在 URL、
状态文件或日志中。

`POST /api/ingest/news` 接受的主体是单篇新闻内容（也可以包在 `{"article": {...}}` 中）：

```json
{
  "id": "可选；必须与 source/page_name 匹配",
  "source": "niji_topics",
  "page_name": "01_123",
  "title": "新闻标题",
  "published_at": "2026-09-17",
  "category": "goods",
  "tags": ["goods"],
  "summary": "摘要",
  "body_markdown": "正文",
  "source_url": "https://www.lovelive-anime.jp/...",
  "source_hash": "来源摘要",
  "images": [
    {
      "kind": "remote",
      "source_url": "https://www.lovelive-anime.jp/.../image.jpg",
      "public_url": "https://<R2_PUBLIC_BASE_URL>/images/news-remote/<sha256>.jpg",
      "alt_text": "图片说明",
      "width": 1280,
      "height": 720,
      "bytes": 123456,
      "sha256": "<64 位十六进制 SHA-256>"
    }
  ]
}
```

请求头为：

```text
Content-Type: application/json
X-Nijidb-API-Key: <API Key>
```

服务端只接受当前实例 `R2_PUBLIC_BASE_URL` 下、当前 `R2_IMAGE_PREFIX` 前缀中的公开地址；
不接受本地路径、数据库文件或数据库覆盖。管理员手动编辑字段仍由现有保护逻辑保留。没有
`public_url` 的图片可以作为同一篇文章中尚未归档的图片提交，已有 R2 地址不会被后续空值刷新
覆盖。

## Worker 环境变量

在 Worker 主机上创建权限为 `600` 的环境文件，以下只是字段示例，不要把真实值提交到 Git：

```text
NIJIDB_BASE_URL=https://your-site.example
NIJIDB_INGEST_API_KEY=<与网站端相同的单独随机值>

R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=nijidb
R2_ACCESS_KEY_ID=<专用 R2 Access Key ID>
R2_SECRET_ACCESS_KEY=<专用 R2 Secret Access Key>
R2_PUBLIC_BASE_URL=https://<R2 公共域名>
R2_IMAGE_PREFIX=images

NEWS_WORKER_STATE_FILE=/home/<user>/.local/state/nijidb-news-worker/state.json
# 可选：扩展官方 CDN 主机，逗号分隔
# NEWS_WORKER_IMAGE_HOSTS=img.example.com
# 可选：官网直连被拒时使用的单条固定代理（不做轮换）
# NEWS_WORKER_PROXY=http://127.0.0.1:17899
```

R2 凭据应使用只允许目标 Bucket 对象读写的专用凭据；Worker 不需要删除对象或替换数据库。
状态文件不含 API Key，但可能包含待提交的公开 R2 地址和错误计数，也应保持权限 `600`。

## 安装与试运行

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install boto3

# 先只检查下一张图片，不下载、不上传、不修改网站
python scripts/remote_news_image_worker.py --once --dry-run

# 处理一张图片，用于验证网络、R2 和 API Key；默认不会等待 30 秒
python scripts/remote_news_image_worker.py --once

# 长期运行：每小时工作、半小时休息
python scripts/remote_news_image_worker.py
```

`--once` 会继续使用状态文件；测试成功后不要删除状态文件，否则会从新闻索引开头重新扫描。
不要同时启动两个相同状态文件的 Worker。若 API 返回 401/503，Worker 会保留已上传但尚未
提交的状态，修复配置后可继续提交；R2 中的孤立对象需要人工按清单处理，不会自动删除。

## systemd 示例

在服务器上把脚本同步到固定目录，例如 `/home/<user>/nijidb-news-worker/`，环境文件放在
`/home/<user>/.config/nijidb-news-worker.env`，并将下面的 unit 保存到
`/home/<user>/.config/systemd/user/nijidb-news-worker.service`，再使用：

```ini
[Unit]
Description=Nijidb remote news image worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/nijidb-news-worker
EnvironmentFile=/home/<user>/.config/nijidb-news-worker.env
ExecStart=/home/<user>/nijidb-news-worker/.venv/bin/python /home/<user>/nijidb-news-worker/scripts/remote_news_image_worker.py
Restart=on-failure
RestartSec=60
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

安装环境文件后执行：

```bash
chmod 600 /home/<user>/.config/nijidb-news-worker.env
systemctl --user daemon-reload
systemctl --user enable --now nijidb-news-worker.service
journalctl --user -u nijidb-news-worker.service -f
```

若服务器使用系统级 systemd，把 unit 放到 `/etc/systemd/system/` 并按该主机的权限执行；不要
把环境文件内容传给 `journalctl`、shell 历史或聊天记录。

## Termux

Termux 没有系统级 systemd，可安装 Python 和 boto3 后在 `tmux`、`termux-services` 或前台
运行同一脚本。把 `NEWS_WORKER_STATE_FILE` 改为 Termux 可写路径（例如
`$HOME/.local/state/nijidb-news-worker/state.json`），并只在 Termux 私有目录保存环境文件。
网络切换、锁屏和 Android 电池策略可能中断任务；脚本会在下次启动时依据状态文件继续。

## 部署顺序

1. 在网站容器的下一次代码部署中注入 `NIJIDB_INGEST_API_KEY`，不要修改现有管理员鉴权。
2. 用专用 R2 凭据和同一个 API Key 配置 Worker，先运行 `--once --dry-run`。
3. 再运行一次 `--once`，确认 R2 对象、网站新闻详情的 `public_url` 和页面封面均正常。
4. 最后才启用长期 systemd/Termux 任务。生产部署仍只替换 `nijidb-web`，不重启 Caddy。
5. 轮换 API Key 或 R2 凭据时先停止 Worker，更新两端权限为 `600` 的环境文件，验证后再启动。
