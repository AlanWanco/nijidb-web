FROM oven/bun:alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN bun install --no-save --ignore-scripts
COPY frontend ./
RUN bun run build

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app ./app
COPY --from=frontend-build /frontend/dist ./frontend/dist

ENV PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
