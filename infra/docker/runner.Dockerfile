# 构建包含 Docker CLI、Git、SSH 与 Runner 可选依赖的执行面镜像。
FROM docker:28-cli AS docker-cli
FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/backend/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl tini && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock* /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev --extra runner --no-install-project
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev --extra runner
RUN mkdir -p /var/lib/devops/workspaces /var/lib/devops/ssh /var/log/devops
WORKDIR /app/backend
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "umask 0007 && exec python -m devops.runner.main"]
