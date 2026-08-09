# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89

# 前端只在构建阶段需要 Node；最终运行镜像不包含 Node/npm。
FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS web-builder

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY web/index.html web/tsconfig*.json web/vite.config.ts ./
COPY web/public/ ./public/
COPY web/src/ ./src/
RUN npm run build


# Python 依赖单独构建；锁文件变化前都可复用这一层。
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project


# 运行阶段只带 Python 生产依赖、业务源码和已编译前端。
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    HOME=/home/dra \
    DRA_DATA_DIR=/data \
    DRA_MAX_ACTIVE_RUNS=1

WORKDIR /app

RUN groupadd --system dra \
    && useradd --system --gid dra --home-dir /home/dra --create-home dra \
    && install -d -o dra -g dra /data

COPY --from=python-builder --chown=dra:dra /app/.venv /app/.venv
COPY --chown=dra:dra src/ /app/src/
COPY --from=web-builder --chown=dra:dra /build/web/dist /app/web/dist
COPY --chown=dra:dra fixtures/demo_run/events.jsonl /app/fixtures/demo_run/events.jsonl

USER dra

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).read()"]

CMD ["uvicorn", "dra.web:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
