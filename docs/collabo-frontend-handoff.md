# /collabo 前端交接

## 本次范围与协作边界

- `/illustrations` 页面保留，主导航和首页入口指向新 `/collabo`。
- 前端集中在 `frontend/src/collabo/`、三个 `Collabo*View.vue` 和两个 `Collabo*` 组件；数据库实现位于
  `app/collabo.py` 和 `app/main.py`。
- 应用启动时会把本地 index / manifest 中尚未入库的记录和图片元数据迁移到 SQLite；手动字段不会被刷新覆盖。

## 已实现

- `/collabo`：每次联动一张卡片，单张封面、日期、标题、合作方、角色标签、图片数；24 项分页，搜索、年份和角色筛选；“全员”角色标签表示全选。
- `/collabo/:slug`：左侧完整立绘画廊、右侧资料和链接；主图选择条、图片说明、原图入口、放大对话框；展示角色标签及多个带标题/描述的开始—结束日期时间段。
- 详情页保留标题、日期含义、合作方、版权标注、备注与来源。相关链接显示标题，不直接显示裸 URL。
- 尚未提供页面标题的额外图片来源显示“来源页面（标题待补充）”，不伪造标题；编辑页可补充。
- 返回一览保留搜索/年份/页码和滚动位置；上下联动使用同一筛选上下文和稳定排序。
- 左右按键与移动端左右滑动切换联动；放大画廊内只切换图片。纵向滚动、双指缩放、边缘返回、表单等不触发翻页。
- CD 详情页复用手机滑动导航，并增加快速切换时的过期请求防护。
- `/admin/collabo`：复用现有管理员登录，提供记录列表及整条记录的管理入口。
- `/admin/collabo/new`、`/admin/collabo/:id`：资料、日期含义、合作方、角色标签、多个带标题/描述的开始—结束日期时间段、版权、备注、来源链接标题与 URL、多图编辑。
- 图片可以排序、选择封面、补充说明/来源；所有图片随联动记录直接展示。
- 数据库保存、保存并进入下一条、批量图片上传接口已接到适配器；未保存离开提示、导出草稿 JSON。
- 日间/夜间及窄屏适配；卡片、主图切换采用轻量动效，支持 `prefers-reduced-motion`。

## 当前数据模式（重要）

1. 优先访问 SQLite 驱动的 `/api/collabo`、`/api/collabo/:slug` 及管理员接口。
2. 只有 API 不存在（404 或旧部署返回 SPA HTML）才读取现有的
   `frontend/src/content/collaborationIllustrations.json` 和 `/api/collaboration-illustrations`。
3. 数据库/网络/鉴权错误不能伪装成预览或空目录；在页面显示错误。
4. 预览模式中的图片直接展示，不沿用“至少 3 张 = 完整收录”的自动结论。
5. 数据库模式支持管理员保存和上传；预览模式不会写数据库或上传资源。
6. 新数据库接口存在时，找不到某条记录就显示不存在，不从旧 JSON 重新带回已删除记录。

### URL 规则

- 正式 URL：`/collabo/20260911-abcdef`。
- 正式 `slug` 由数据库端生成：确认的公布/开启日 + 随机 6 位小写十六进制字符；唯一约束、碰撞重试。
- 编辑日期不自动更换已发布 slug；如有改址需求，后端保留旧 slug 别名。
- **兼容预览仅使用 `first_seen + 旧 ID 前 6 位` 的稳定临时别名，不是最终随机 slug。**
- 旧 `first_seen` 保留“首次公开”含义，不能未核实就当作活动开启日。

## 暂定接口

### 公开读取

`GET /api/collabo?q=&year=&tags=&page=1&page_size=24`

```json
{
  "items": [],
  "total": 271,
  "years": ["2026", "2025"],
  "source": { "title": "原 Wiki 页面标题", "url": "https://example.org/wiki" }
}
```

- 列表只需要摘要和封面地址，不应返回每条记录全部大图；`thumbnail_url` 仅为历史兼容字段。
- 推荐按日期倒序、相同日期按固定 ID 排序；total 是当前筛选后的总数，years 是完整可用年份。

`GET /api/collabo/:slug?q=&year=&tags=`

```json
{
  "item": {
    "id": "stable-database-id",
    "slug": "20260911-abcdef",
    "title": "联动标题",
    "date": "2026-09-11",
    "date_kind": "announced",
    "partners": ["合作方"],
    "tags": ["ayumu", "lanzhu"],
    "periods": [
      {
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "title": "第一期",
        "description": "活动说明"
      }
    ],
    "credit": "原版权标注",
    "note": "保留换行的原始备注",
    "links": [{ "title": "对应页面的真实标题", "url": "https://example.org/news" }],
    "cover_image_id": "asset-id",
    "cover_url": "https://r2-public.example/images/collabo/original.png",
    "thumbnail_url": "",
    "image_count": 1,
    "updated_at": "2026-09-11T00:00:00Z",
    "images": [
      {
        "id": "asset-id",
        "url": "https://r2-public.example/images/collabo/original.png",
        "thumbnail_url": "",
        "caption": "图片说明",
        "alt": "成员与立绘描述",
        "source_url": "https://example.org/original.png",
        "source_page": "https://example.org/news",
        "source_title": "对应来源页面标题",
        "width": 1000,
        "height": 1600
      }
    ]
  },
  "previous": { "id": "previous-id", "slug": "20260912-123abc", "title": "上一条" },
  "following": null,
  "source": { "title": "原 Wiki 页面标题", "url": "https://example.org/wiki" }
}
```

- `previous` / `following` 与列表排序一致；到边界返回 null。
- `date_kind`：`announced` / `starts` / `first_seen`。
- `tags` 使用固定角色 ID；`tags=ayumu,lanzhu` 按任一角色匹配，省略 tags 表示全选。
- `periods` 是多个 `{ start_date, end_date, title, description }`；每段日期必填且结束日期不能早于开始日期，标题和描述至少填写一项。
- 原 Wiki 完整数据/快照、原始 ID 与采集 provenance 仍由数据库保留；前端不覆盖这些原始字段。

### 管理接口（必须服务端鉴权）

- `GET /api/admin/collabo`：同列表参数与响应。
- `GET /api/admin/collabo/:id`：同详情响应，包含全部图片，队列相邻项用数据库 ID。
- `POST /api/admin/collabo`：创建，body 是可编辑 item；正式 ID 与随机 slug 服务端生成。
- `PATCH /api/admin/collabo/:id`：修改可编辑字段，不删除未传入的原始元数据。
- 保存返回 `{ "item": 完整保存结果 }`；建议使用 `updated_at` 做并发修改检查，不静默覆盖其他编辑。
- `POST /api/admin/collabo/assets`：multipart，`files` 字段可多项，返回 `{ "images": [图片对象] }`；服务端也兼容单文件原始请求。
  前端限制 JPEG/PNG/WebP、单张 20 MB、每批 16 张；服务端检查内容、大小、实际编码和管理员权限。
- R2 上传在后端执行，浏览器不接触 R2 密钥。上传成功后才允许把返回对象加入记录；未关联资产可延迟回收。
- 仅移除记录中的图片关联，不应立即删除被其他联动引用的 R2 原图。
- 服务器端 URL 抓取应限制为公网 HTTP(S)，防止 SSRF、重定向进入私网，设置超时/响应大小限制。

## 图片展示

- 当前不生成或维护批量缩略图，不上传、压缩或覆盖采集原图。
- 前端保留对历史 `thumbnail_url` 的兼容读取；没有缩略图时直接回退到原图，列表分页且每卡仅加载一图，图片使用 lazy loading。

## 验证

- `cd frontend && npm run build`
- `node --test frontend/tests/collabo-model.test.mjs`
- `git diff --check`
- 无头浏览器回归：`frontend/tests/collabo-browser.cjs`。
  使用本地 JSON / manifest 作为只读 fixture，**所有 API 均被 mock，不连接真实数据库**。
  需已有 Chrome 和 Playwright；可以在 `/tmp` 安装 Playwright，不修改项目依赖：

```sh
# 在单独终端运行，仅启动前端
node frontend/node_modules/vite/bin/vite.js frontend --host 127.0.0.1 --port 15173 --strictPort
# PLAYWRIGHT_MODULE 指向外部安装的 playwright 模块目录
PLAYWRIGHT_MODULE=/tmp/your-tools/node_modules/playwright node frontend/tests/collabo-browser.cjs
```

- 回归覆盖：网格分页容量、昼夜模式 DOM、详情/灯箱按键、关闭及滚动解锁、返回列表位置、
  预览禁用数据库写入、草稿导出、mock 数据库保存、手机无横向溢出、纵向滚动不翻页、联动/CD 左右滑动。
- 浏览器回归不代表真实 R2 上传、数据库持久化和随机 slug 分配已验收；这些待后端接入后联调。
- 数据库迁移脚本：`scripts/seed_collabo_database.py`；生产部署前应使用源 index / manifest 对现有数据卷执行一次导入并核对数量。
