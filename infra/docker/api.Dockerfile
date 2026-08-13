# 构建 Vue 控制台并生成无 Docker 权限的 FastAPI API 运行镜像。
FROM node:24-bookworm-slim AS frontend-build
WORKDIR /src
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN corepack enable \
    && pnpm install --frozen-lockfile --filter devops-console-web
COPY frontend/ ./frontend/
RUN pnpm --filter devops-console-web build

FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/backend/.venv/bin:$PATH
RUN groupadd --gid 10001 devops \
    && useradd --uid 10001 --gid devops --home-dir /app --no-create-home --shell /usr/sbin/nologin devops
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock* /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev --no-install-project
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev
COPY templates/ /app/templates/
COPY --from=frontend-build /src/frontend/dist /app/frontend/dist
RUN mkdir -p /var/lib/devops /var/log/devops && chown -R devops:devops /app /var/lib/devops /var/log/devops
USER devops
WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn devops.api.main:app --host 0.0.0.0 --port 8000 --workers 1"]
