# syntax=docker/dockerfile:1.7
# Node.js 多阶段构建模板；平台复制后允许管理员按可信项目修改。
FROM node:24-bookworm-slim AS build
WORKDIR /workspace
RUN corepack enable
COPY package.json pnpm-lock.yaml* package-lock.json* yarn.lock* ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; \
    elif [ -f yarn.lock ]; then yarn install --immutable; \
    else npm ci; fi
COPY . .
RUN if [ -f pnpm-lock.yaml ]; then pnpm build; elif [ -f yarn.lock ]; then yarn build; else npm run build; fi

FROM node:24-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
RUN useradd --system --uid 10001 --create-home app
COPY --from=build --chown=app:app /workspace/package.json ./
COPY --from=build --chown=app:app /workspace/node_modules ./node_modules
COPY --from=build --chown=app:app /workspace/dist ./dist
USER 10001
EXPOSE 3000
CMD ["node", "dist/index.js"]
