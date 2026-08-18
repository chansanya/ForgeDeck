# 目录与模块说明

本文档描述 Light DevOps 当前代码结构、模块职责、运行时数据目录以及可以安全重建的生成物。这里写的是现有实现，不是路线图。

## 总体分层

```text
frontend
   |
   | REST / SSE / WebSocket
   v
backend.api ------------------ /mcp / webhooks
   |
   | SQLAlchemy / SQLite persistent queue
   v
backend.runner
   |                     |
   | Docker SDK/CLI      | AsyncSSH/SFTP/PTY
   v                     v
local Docker        target servers
```

依赖方向：

- 前端只通过 HTTP、SSE 和 WebSocket 访问 API，不读取数据库。
- API 路由调用 `services`、`db`、`domain` 和外部集成，不直接访问 Docker Socket。
- Runner 通过持久任务队列领取任务，再调用构建、Docker、SSH 和部署模块。
- API 与 Runner 共享领域模型和数据库，但使用不同进程与权限。

## 仓库根目录

```text
devops/
├── backend/                Python API、Runner、迁移和测试
├── frontend/               Vue 3 管理控制台
├── docs/                   项目文档
├── infra/                  Docker 与 systemd 发布文件
├── templates/              Java、Node.js、Python 和 Compose 模板
├── skills/                 AI Observer/Operator Skills
├── scripts/                仓库级开发与 CI 校验脚本
├── .github/                GitHub Actions CI
├── AGENTS.md               Agent 与开发者代码规范
├── docker-compose.yml      Compose 部署入口
├── .env.example            Compose 生产配置模板
├── .node-version           Node.js 版本约束文件
├── package.json            pnpm 工作区根元数据
├── pnpm-workspace.yaml     前端工作区声明
├── pnpm-lock.yaml          Node.js 冻结依赖
└── README.md               项目总览、安装和运维入口
```

| 路径 | 作用 |
|---|---|
| `backend/` | FastAPI 控制面、Runner 执行面、数据库迁移和后端测试 |
| `frontend/` | Vue 3、TypeScript、Naive UI 管理控制台 |
| `docs/` | 架构、目录和后续运维文档 |
| `infra/docker/` | API 与 Runner 的生产镜像定义 |
| `infra/systemd/` | Linux 安装脚本、服务 unit 和环境变量模板 |
| `templates/` | Web 控制台可读取并复制的项目构建模板 |
| `skills/` | Codex/AI 使用的 MCP 工作流与安全边界 |
| `scripts/` | 文件头说明等跨后端、前端的仓库级检查 |
| `.github/workflows/ci.yml` | 后端、前端和发布检查 |
| `AGENTS.md` | 文件说明、关键注释、模块边界和验证要求 |
| `docker-compose.yml` | `volume-init`、`api`、`runner` 和持久卷编排 |
| `.dockerignore` | 排除依赖、缓存、数据和密钥，缩小 Docker 构建上下文 |
| `.gitignore` | 排除虚拟环境、依赖、构建产物和运行时数据 |

## 后端 `backend/`

```text
backend/
├── src/devops/             应用源码
├── alembic/                数据库迁移
├── tests/                  后端与 Runner 测试
├── pyproject.toml          Python 项目和工具配置
├── uv.lock                 Python 冻结依赖
├── alembic.ini             Alembic 配置
├── .env.example            本地开发配置模板
└── README.md               后端开发说明
```

### 应用顶层模块 `backend/src/devops/`

| 文件 | 作用 |
|---|---|
| `main.py` | `devops-api` 控制台入口，启动 Uvicorn |
| `cli.py` | `init-admin` 管理员初始化和密码重置命令 |
| `config.py` | Pydantic Settings、环境变量解析和生产占位值校验 |
| `schemas.py` | REST API 请求/响应 Pydantic 模型与输入安全校验 |
| `security.py` | Argon2 密码、JWT、Fernet 凭据加解密和主密钥管理 |
| `services.py` | 配置快照、参数哈希、流水线创建等跨路由业务逻辑 |
| `logging_config.py` | structlog 结构化日志配置 |

### API `backend/src/devops/api/`

| 文件/目录 | 作用 |
|---|---|
| `main.py` | 创建 FastAPI、生命周期、CORS、Trace ID、MCP 和前端挂载 |
| `deps.py` | 当前管理员、数据库 Session、SecretManager 等依赖注入 |
| `static.py` | 托管 Vue `dist`，支持 SPA history fallback |
| `routes/` | `/api/v1` 和 `/webhooks` 的类型化路由 |

API 路由：

| 路由模块 | 职责 |
|---|---|
| `auth.py` | 登录、当前用户、修改密码 |
| `dashboard.py` | 首页统计摘要 |
| `credentials.py` | Git、SSH、Registry、Webhook、SMTP 等加密凭据 |
| `projects.py` | 项目、Dockerfile、环境和 Compose 配置 |
| `servers.py` | 服务器登记、更新、主机密钥扫描和指标查询 |
| `templates.py` | 读取 `templates/` 中的项目模板 |
| `pipelines.py` | 手动运行、运行列表、取消、日志和 SSE |
| `deployments.py` | 部署申请、部署记录和回滚申请 |
| `scripts.py` | 版本化脚本库和脚本执行申请 |
| `approvals.py` | 操作申请审批、拒绝和参数哈希复核 |
| `audit.py` | 审计日志查询 |
| `notifications.py` | 钉钉、Webhook、SMTP 通知通道和测试 |
| `mcp_tokens.py` | 短期、带 scope 的 MCP Token 签发与吊销 |
| `runner_proxy.py` | 把 Docker 请求和 SSH WebSocket 安全代理到 Runner |
| `webhooks.py` | GitLab、GitHub、Gitee 验签、过滤和 delivery 去重 |

### 数据库 `backend/src/devops/db/`

| 文件 | 作用 |
|---|---|
| `engine.py` | 异步 SQLAlchemy 引擎；SQLite WAL、外键和 `busy_timeout` |
| `repositories.py` | 领域对象和任务的仓储查询 |
| `uow.py` | Unit of Work，控制短事务提交与回滚 |
| `results.py` | 数据库操作结果类型 |

### 领域 `backend/src/devops/domain/`

`models.py` 定义 SQLAlchemy 模型、枚举和状态字段，包括管理员、凭据、项目、环境、服务器、指标、流水线、阶段、任务、部署、脚本、通知、MCP Token、审批和审计。

领域模型是 API 与 Runner 的共享契约。新增状态时必须同时检查状态迁移、仓储、Pydantic Schema、Runner handler 和前端类型，不能只在数据库模型上拍脑袋加个字符串。

### 外部集成 `backend/src/devops/integrations/`

| 文件 | 作用 |
|---|---|
| `mcp.py` | Streamable HTTP MCP 工具、Bearer scope 和输出字段白名单 |
| `notifications.py` | 钉钉机器人、HMAC Webhook、SMTP 邮件发送 |

### Runner `backend/src/devops/runner/`

| 文件 | 作用 |
|---|---|
| `main.py` | 启动任务 Worker、指标 Worker、维护调度器和内部 API |
| `engine.py` | 任务轮询、租约、心跳、取消、重试和终态提交 |
| `store.py` | SQLAlchemy 持久任务队列、CAS、恢复和 JSONL 日志索引 |
| `state.py` | 流水线、部署和任务的合法状态迁移 |
| `handlers.py` | Pipeline、Deployment、Script、Metrics 四类任务总编排 |
| `source.py` | Git checkout、commit 固化、协议限制和路径逃逸防护 |
| `build.py` | Buildx 命令、Registry 推送和 digest 元数据读取 |
| `deploy.py` | Compose 上传、pending 快照、健康检查、对账和回滚 |
| `docker.py` | Docker SDK 与 Compose 管理、危险操作预览 |
| `ssh.py` | AsyncSSH、主机指纹、命令、SFTP 和 PTY |
| `process.py` | 使用参数数组执行 Git、Buildx、Compose，禁止 `shell=True` |
| `credentials.py` | 解密任务凭据、Git askpass 和临时 Docker config |
| `metrics.py` | CPU、内存、磁盘和网络采集 |
| `logs.py` | 运行日志脱敏、追加和读取 |
| `scheduler.py` | 周期指标任务、过期日志和审计清理 |
| `internal_api.py` | 仅供 API 调用的 Runner HTTP/WebSocket 接口 |
| `contracts.py` | Runner 请求、结果和协议类型 |

### 数据库迁移 `backend/alembic/`

| 文件 | 作用 |
|---|---|
| `env.py` | Alembic 异步迁移环境 |
| `script.py.mako` | 新迁移文件模板 |
| `versions/0001_initial.py` | 初始控制面 Schema |
| `0002_notifications_and_mcp_tokens.py` | 通知通道与 MCP Token |
| `0003_project_registry_credential.py` | 项目绑定 Registry 凭据 |
| `0004_deployment_previous_target.py` | 部署绑定精确上一版本 |
| `0005_environment_deploy_target.py` | 同服务器部署目录唯一约束 |

迁移文件属于产品升级链，不能因为“表已经存在”就删。删迁移相当于把老用户升级路线炸了，然后假装路一直都这么平。

### 测试 `backend/tests/`

- 顶层测试覆盖认证、资源 API、审批、Webhook、SSE、MCP、通知、迁移和仓储契约。
- `runner/` 覆盖构建、部署、Docker、SSH、任务租约、状态机、进程调用、日志和崩溃恢复。
- `conftest.py` 提供临时 SQLite、FastAPI 测试应用和公共 fixture。

## 前端 `frontend/`

```text
frontend/
├── src/                    Vue/TypeScript 源码
├── public/                 Vite 原样复制的站点静态资源（含 favicon.ico）
├── index.html              Vite HTML 入口
├── vite.config.ts          代理、分包和构建配置
├── package.json            前端依赖与命令
├── tsconfig*.json          TypeScript 配置
├── .env.example            Vite 环境变量模板
├── .prettierrc.json        Vue/CSS 格式化规则
├── .prettierignore         格式化排除项
└── README.md               前端开发说明
```

### 前端源码 `frontend/src/`

| 目录/文件 | 作用 |
|---|---|
| `main.ts` | 创建 Vue、Pinia、Router 和 Naive UI 应用 |
| `App.vue` | 根组件和全局 Naive UI Provider |
| `api/client.ts` | REST、SSE、Token 和 SSH 会话请求封装 |
| `api/types.ts` | 与后端 Schema 对应的 TypeScript 类型 |
| `router/index.ts` | 路由、懒加载、登录守卫和页面标题 |
| `stores/session.ts` | 管理员登录状态和 Token 持久策略 |
| `layouts/AppShell.vue` | 登录后侧边导航、顶栏和内容框架 |
| `components/` | 通用卡片、状态、空状态、图表和项目编辑弹窗 |
| `views/` | 与菜单对应的业务页面 |
| `styles/base.css` | 全局变量、基础布局和响应式规则 |
| `utils/` | 格式化、项目配置转换及单元测试 |
| `env.d.ts` | Vite 环境变量类型声明 |

页面模块：

| 页面 | 职责 |
|---|---|
| `DashboardView.vue` | 资源和任务总览 |
| `ProjectsView.vue` | 项目、Dockerfile 和环境管理 |
| `PipelinesView.vue` | 流水线运行列表 |
| `RunDetailView.vue` | 阶段状态、SSE 日志和取消 |
| `ServersView.vue` | 服务器、主机指纹和指标 |
| `DockerView.vue` | 容器、镜像、网络、卷和 Compose |
| `DeploymentsView.vue` | 部署申请、历史和回滚 |
| `ScriptsView.vue` | 版本化 SSH 脚本 |
| `CredentialsView.vue` | 加密凭据管理 |
| `ApprovalsView.vue` | 操作申请审批 |
| `IntegrationsView.vue` | 通知通道和 MCP Token |
| `AuditView.vue` | 审计日志 |
| `SshTerminalView.vue` | xterm.js SSH PTY |
| `LoginView.vue` | 管理员登录 |

Vue 单文件组件统一使用：

```vue
<template>
  <!-- markup -->
</template>

<script setup lang="ts">
// typed behavior
</script>

<style scoped>
.selector {
  display: block;
}
</style>
```

禁止把 CSS 规则压成一行。执行 `corepack pnpm --filter devops-console-web format` 自动格式化，CI 使用 `format:check` 阻止格式回退。

## 发布文件 `infra/`

### Docker

- `api.Dockerfile`：Node 24 构建前端，Python 3.13 运行 API；不包含 Docker Socket。
- `runner.Dockerfile`：安装 Docker CLI/Compose 插件、Git、OpenSSH、AsyncSSH 和 Runner 依赖。

### systemd

- `install.sh`：校验 Linux amd64、Node、pnpm、uv、Buildx、Compose 和 docker group，复制源码并构建前端。
- `devops-api.service`：以 `devops-api` 用户运行，启动前执行 Alembic。
- `devops-runner.service`：以 `devops-runner` 用户运行，并加入 `docker` 组。
- `devops.env.example`：systemd 生产配置模板。

## 项目模板 `templates/`

| 目录 | 作用 |
|---|---|
| `java-maven/` | Maven 多阶段 Dockerfile |
| `java-gradle/` | Gradle 多阶段 Dockerfile |
| `node/` | Node.js 多阶段 Dockerfile |
| `python/` | Python 多阶段 Dockerfile |
| `compose/` | 通用 Compose 部署模板 |

每个语言模板的 `template.json` 提供模板 ID、名称和元数据，`Dockerfile` 是可复制、可编辑的初始内容。

## AI Skills `skills/`

| 目录 | 作用 |
|---|---|
| `devops-observer/` | 只读查询服务器、指标、Docker、流水线、部署和日志 |
| `devops-operator/` | 创建构建、部署、回滚和脚本待审批申请 |

- `SKILL.md` 描述模型工作流、安全边界和输出要求。
- `agents/openai.yaml` 描述 Skill 展示信息及 MCP Streamable HTTP 依赖。
- Operator 没有审批工具，也不能直接执行任意 SSH 或原始 Docker 命令。

## CI `.github/`

`workflows/ci.yml` 包含：

- `backend`：Ruff、Pyright、Pytest、SQLite/PostgreSQL 迁移。
- `frontend`：Prettier、Vue TypeScript、Vitest、Vite 构建。
- `release`：systemd 脚本、Skill 元数据、Compose 配置和 Docker 镜像构建。

## 运行时与生成目录

以下内容不是源码：

| 路径 | 内容 | 清理策略 |
|---|---|---|
| `backend/.venv/` | uv 创建的 Python 环境 | 可用 `uv sync` 重建 |
| `node_modules/` | pnpm 工作区依赖 | 可用 `pnpm install` 重建 |
| `frontend/node_modules/` | 前端依赖链接或历史安装残留 | 可重建；不要提交 |
| `frontend/dist/` | Vite 生产构建 | 可用 `pnpm build` 重建 |
| `backend/data/` | 本地 SQLite 和工作区 | 运行后是业务数据，不能随便删 |
| `backend/logs/` | 本地流水线 JSONL 日志 | 按保留策略清理 |
| `backend/secrets/` | 本地主密钥 | 不能丢失，必须与数据库分开备份 |
| `backend/data/workspaces/` | Runner 默认 checkout 和构建工作区，属于 `backend/data/` 子目录 | 无活动任务时可清理；不要连同数据库一起误删 |
| `.ruff_cache/`、`.pytest_cache/`、`__pycache__/` | 工具缓存 | 可安全重建 |
| `backend/.venv313/` | 历史或额外的 Python 3.13 虚拟环境 | 确认当前命令未使用后可重建 |
| `frontend/.cache/` | 前端工具缓存 | 可安全重建 |
| `.pnpm-store/` | 项目内 pnpm Store 或残留 | 确认不是当前 Store 后可清理 |
| `.git/` | Git 元数据 | 正常仓库必须保留；空目录不是有效仓库 |
| `.agents/` | 当前项目未使用的本地空目录 | 为空时可删除 |

判断一个目录能不能删，先看它是“锁文件可重建的依赖/缓存”，还是“数据库、日志、主密钥”。把这两类混着清理，是最省时间的永久丢数据方案。

## 新增模块时的放置规则

- 新 REST 资源放入 `api/routes/`，通用业务逻辑放 `services.py` 或独立 service 模块。
- 新数据库访问先扩展 repository/UoW，不要在路由里散落事务代码。
- 新高权限能力必须进入 Runner，并通过类型化内部接口或持久任务调用。
- 新外部通知或协议适配放入 `integrations/`。
- 新前端业务页面放 `views/`，可复用 UI 放 `components/`，传输类型放 `api/types.ts`。
- 新数据库字段必须提供 Alembic 迁移和升级测试。
- 新危险操作必须带影响预览、审计和明确确认，不能偷偷塞一个“万能 execute”接口。
