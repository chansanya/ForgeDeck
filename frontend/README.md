# Light DevOps Web Console

Vue 3 + TypeScript + Vite + Naive UI 管理界面，直接对接后端 `/api/v1`，不包含演示数据或 Mock API。

完整目录职责见[目录与模块说明](../docs/project-structure.md)。
文件说明、关键逻辑注释和验证规则见[Agent 开发规范](../AGENTS.md)。

## 本地开发

corepack 是 Node.js 内置的包管理器版本管理器，读 `package.json` 的 `packageManager` 字段自动切换到指定版本的 pnpm，无需全局安装。从 Node 16.9 起随 Node 一起发布，执行前需 `corepack enable` 激活一次。

从仓库根目录执行：

```bash
corepack pnpm install --frozen-lockfile --filter devops-console-web
corepack pnpm --filter devops-console-web dev
```

命令逐段拆解：

| 参数 | 含义 |
|---|---|
| `corepack` | Node.js 内置工具，自动按 package.json 指定的版本调用 pnpm |
| `pnpm` | 包管理器（类似 npm/yarn，支持 workspace） |
| `install` | 安装依赖到 `node_modules/` |
| `--frozen-lockfile` | 严格按 `pnpm-lock.yaml` 安装，不更新锁文件，保证版本一致 |
| `--filter devops-console-web` | pnpm workspace 过滤器，只操作 `frontend/` 这个包（包名在 `frontend/package.json` 的 `name` 字段定义） |
| `dev` | 执行该包 `package.json` 里 `"scripts": {"dev": "vite --host 0.0.0.0"}`，启动 Vite 开发服务器 |

也可以先 `cd frontend` 再去掉 `--filter`，效果完全一样：

```bash
cd frontend
pnpm install --frozen-lockfile   # 装依赖
pnpm dev                         # 启动 Vite（端口 5173）
```

默认地址为 `http://127.0.0.1:5173`。Vite 将 `/api`、`/webhooks` 和 `/mcp` 代理到 `http://127.0.0.1:8000`；可复制 `frontend/.env.example` 并修改 `VITE_DEV_API_TARGET`。

## Vue 文件结构

所有组件统一使用：

```vue
<template>
  <!-- markup -->
</template>

<script setup lang="ts">
// behavior
</script>

<style scoped>
.selector {
  display: block;
}
</style>
```

CSS 必须保持多行可读格式，禁止把整条规则压成一行。图标统一使用 Lucide。

## 格式化、测试与构建

```bash
corepack pnpm --filter devops-console-web format
corepack pnpm --filter devops-console-web format:check
corepack pnpm --filter devops-console-web typecheck
corepack pnpm --filter devops-console-web test
corepack pnpm --filter devops-console-web build
```

| 命令 | 说明 |
|---|---|
| `format` | 用 Prettier 自动格式化 Vue 和 CSS 文件 |
| `format:check` | 只检查不修改，CI 用它阻止未格式化的代码提交 |
| `typecheck` | vue-tsc 类型检查，捕获模板和脚本中的类型错误 |
| `test` | Vitest 单元测试，覆盖工具函数和配置转换 |
| `build` | 生产构建，先 typecheck 再 Vite 打包，输出到 `frontend/dist` |

生产构建输出到 `frontend/dist`，默认不生成 sourcemap。只有在受控调试环境中才设置 `VITE_BUILD_SOURCEMAP=true`。

API 可以通过 `DEVOPS_FRONTEND_DIR` 直接托管 `dist`；也可以交给独立 Web Server，但必须把 `/api/v1`、`/webhooks`、`/mcp` 和 SSH WebSocket 正确反向代理到 API。

## 实时协议

- 流水线日志：带 Bearer Token 的 `fetch()` SSE，支持 `Last-Event-ID`、有界缓冲和断线重连。
- SSH 终端：先通过 REST 获取 60 秒有效的一次性会话地址，再由 xterm.js 连接 WebSocket；输入和终端尺寸使用 JSON 控制帧。
