# 官方新闻轮询 + 图片归档共用镜像
#
# 依赖由 uv 按 uv.lock 还原，与站点本体使用同一套锁定依赖；
# 只复制 app/ 与 scripts/，不打包前端和数据库。
FROM python:3.12-slim AS dependencies

COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project


FROM python:3.12-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=dependencies /app/.venv ./.venv
COPY app ./app
COPY scripts ./scripts

# 状态文件目录，由 compose 挂载具名卷；容器以非 root 运行
RUN mkdir -p /state && chown 1000:1000 /state

USER 1000:1000

ENTRYPOINT ["/app/.venv/bin/python"]
# 默认跑内容轮询；补档时用 command 覆盖为 scripts/remote_news_image_worker.py
CMD ["scripts/local_official_news_poller.py"]
