FROM oven/bun:alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN bun install --no-save --ignore-scripts
COPY frontend ./
RUN bun run build

FROM python:3.12-slim AS python-dependencies

COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim

WORKDIR /app
COPY --from=python-dependencies /app/.venv ./.venv
COPY app ./app
COPY --from=frontend-build /frontend/dist ./frontend/dist

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
