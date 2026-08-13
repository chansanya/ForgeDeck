# Light DevOps

Light DevOps 是面向个人和小团队的轻量级、自托管 Docker/Compose 构建部署平台。平台采用 API 与 Runner 双进程设计：API 负责认证、配置、审批和审计，只有 Runner 可以访问 Docker Socket、SSH 凭据和目标服务器。

v1 面向单管理员、局域网/VPN、Linux amd64、可信仓库和可信 Dockerfile。SQLite WAL 是唯一正式生产数据库；PostgreSQL 仅在 CI 中持续验证兼容性。

## 文档导航

- [Agent 开发规范](AGENTS.md)
- [目录与模块说明](docs/project-structure.md)
- [后端开发说明](backend/README.md)
- [后端测试说明](backend/tests/README.md)
- [前端开发说明](frontend/README.md)
- 启动 API 后访问 `/docs` 查看 OpenAPI 文档
- [Observer Skill](skills/devops-observer/SKILL.md) 与 [Operator Skill](skills/devops-operator/SKILL.md) 分别描述 AI 查询和操作申请边界

## 核心能力

- GitLab、GitHub、Gitee Webhook 与手动流水线
- Java Maven、Java Gradle、Node.js、Python 模板和自定义 Dockerfile
- Buildx 构建、OCI Registry 推送、镜像 digest 固化
- Docker Compose 部署、健康检查、失败回滚和任务恢复
- SSH 命令、SFTP、Web PTY 和版本化部署脚本
- 容器、镜像、网络、卷、Compose 与磁盘空间管理
- CPU、内存、磁盘、网络当前值和最近 24 小时指标
- SSE 流水线日志、追加式审计、钉钉/Webhook/SMTP 通知
- Streamable HTTP MCP 与 Observer/Operator Skills

## 架构

```text
Vue Web UI / REST / Webhook / MCP
                 |
        FastAPI API（无 Docker 权限）
                 |
       SQLite 持久队列与配置快照
                 |
       Python Runner（Docker + SSH）
          |                    |
 本机 Docker/BuildKit     AsyncSSH 目标服务器
                               |
                       Docker Compose V2
```

- API 和 Runner 使用同一套领域模型与数据库，但以独立进程运行。
- API 容器不得挂载 `/var/run/docker.sock`。
- Runner 不对公网发布端口；API 使用至少 32 字符的 `DEVOPS_INTERNAL_TOKEN` 调用 Runner 内部接口。
- 任务使用数据库租约、心跳、CAS 状态更新和持久 checkpoint，不能用进程内队列替代。
- 同一项目构建并发为 1，同一环境部署并发为 1；一个环境固定绑定一台服务器。

流水线执行顺序：

```text
Webhook 验签与去重
-> 固化 commit SHA 和配置快照
-> checkout
-> buildx 构建并推送镜像
-> 记录 image digest
-> 远端预检
-> 上传 Compose 和环境配置
-> 使用 image@sha256:digest 部署
-> 健康检查
-> 成功或恢复上一 revision
```

## 运行要求

### 控制机

- 生产环境：Linux amd64/x86_64
- Python 3.13
- Node.js 24 LTS、Corepack、pnpm 10.14.0
- uv 0.11.16 或更高版本
- Docker Engine、Docker Buildx、Docker Compose V2 2.20.0 或更高版本
- 建议从 2 核 CPU、4 GiB 内存起步，默认构建并发为 1

### 目标服务器

- Linux、Docker Engine、Docker Compose V2 2.20.0 或更高版本
- 部署 SSH 用户具备 Docker 权限
- HTTP 健康检查需要 `curl`
- TCP 健康检查需要兼容 `nc -z -w` 的 Netcat，例如 `netcat-openbsd`
- Compose healthcheck 和受控命令检查不依赖 `curl` 或 Netcat

## 本地开发

以下命令均从仓库根目录执行。本地后端使用 `backend/.env`，根目录 `.env` 只供 Docker Compose 使用，两者不能混用。

### 1. 后端

安装 Python 3.13 并创建冻结依赖环境：

```bash
uv python install 3.13                          # 安装 Python 3.13 解释器
cp backend/.env.example backend/.env            # 从模板创建本地配置，按需修改密码和 Token
uv --directory backend sync --python 3.13 --all-extras --dev --frozen  # 按 uv.lock 安装全部依赖
uv --directory backend run alembic upgrade head  # 执行数据库迁移到最新版本
uv --directory backend run python -m devops.cli init-admin  # 创建初始管理员账号
```

Windows PowerShell 复制配置文件：

```powershell
Copy-Item backend/.env.example backend/.env
```

修改 `backend/.env`，确保 API 与 Runner 使用相同且至少 32 字符的 `DEVOPS_INTERNAL_TOKEN`。分别启动两个进程：

```bash
uv --directory backend run uvicorn devops.api.main:app --host 0.0.0.0 --port 8000 --reload  # 启动 API，开发模式热重载
```

```bash
uv --directory backend run python -m devops.runner.main  # 启动 Runner，轮询任务队列
```

### 2. 前端

```bash
corepack pnpm install --frozen-lockfile --filter devops-console-web  # 按锁文件安装前端依赖
corepack pnpm --filter devops-console-web dev                       # 启动 Vite 开发服务器（端口 5173）
```

本地访问入口：

| 地址 | 作用 |
|---|---|
| `http://127.0.0.1:5173` | Vue 开发服务器 |
| `http://127.0.0.1:8000/health` | API 健康检查 |
| `http://127.0.0.1:8000/docs` | OpenAPI 文档 |
| `http://127.0.0.1:8000/mcp` | MCP Streamable HTTP 入口 |

## Docker Compose 部署

1. 复制生产配置模板并替换所有 `replace-*` 占位值：

   ```bash
   cp .env.example .env
   ```

2. 至少设置独立随机的管理员初始密码和 `DEVOPS_INTERNAL_TOKEN`，并核对 MCP Host、Origin、CORS 与 HTTP 端口。

3. 校验并启动：

   ```bash
   docker compose --env-file .env config --quiet  # 校验配置和变量是否合法
   docker compose up -d --build                    # 构建镜像并在后台启动 API 和 Runner
   docker compose logs -f api runner               # 实时跟踪 API 和 Runner 日志
   ```

4. 默认访问 `http://<控制机>:8000`。首次登录后立即修改管理员密码，并从 `.env` 删除 `DEVOPS_ADMIN_INITIAL_PASSWORD` 后重启服务。

Compose 使用以下持久卷：

| 卷/目录 | 内容 | 备份要求 |
|---|---|---|
| `devops-data` | SQLite、指标、配置和任务状态 | 必须备份 |
| `devops-secrets` | 凭据主密钥 | 必须单独备份 |
| `devops-logs` | 流水线 JSONL 日志 | 按审计要求备份 |
| `devops-workspaces` | checkout 和构建工作区 | 可重建，通常无需备份 |
| `${DEVOPS_SSH_DIR:-./data/ssh}` | Runner 的可选 SSH 辅助目录挂载 | 按实际使用决定 |

## systemd 部署

systemd 安装器仅支持 Linux amd64/x86_64，要求预先存在 `docker` 用户组。以 root 运行：

```bash
sudo ./infra/systemd/install.sh                           # 构建前端、安装 Python 环境、注册系统服务
sudoedit /etc/light-devops/devops.env                     # 编辑生产环境变量（密码、Token、路径等）
sudo systemctl enable --now devops-api devops-runner      # 设置开机自启并立即启动 API 和 Runner
```

固定安装位置：

| 路径 | 内容 |
|---|---|
| `/opt/light-devops` | 应用、前端构建和 Python 环境 |
| `/etc/light-devops/devops.env` | 生产配置 |
| `/etc/light-devops/master.key` | 凭据主密钥 |
| `/var/lib/light-devops` | SQLite 和 Runner 工作区 |
| `/var/log/light-devops` | 流水线日志 |

查看日志：

```bash
journalctl -u devops-api -u devops-runner -f
```

## 关键配置

配置由 `backend/src/devops/config.py` 定义，生产模板位于 `.env.example` 和 `infra/systemd/devops.env.example`，本地模板位于 `backend/.env.example`。

| 变量 | 作用 |
|---|---|
| `DEVOPS_ENVIRONMENT` | `development` 或 `production`；生产模式拒绝占位密码和占位 Token |
| `DEVOPS_DATABASE_URL` | SQLAlchemy 数据库地址；v1 正式生产只支持 SQLite |
| `DEVOPS_AUTO_CREATE_SCHEMA` | 开发环境可自动建表；生产应使用 Alembic 并设为 `false` |
| `DEVOPS_DATA_DIR` | 应用数据目录 |
| `DEVOPS_LOG_DIR` | 流水线日志目录 |
| `DEVOPS_WORKSPACE_DIR` | Runner checkout 和构建工作区 |
| `DEVOPS_SECRET_KEY_PATH` | Fernet 主密钥文件；必须与数据库分开备份 |
| `DEVOPS_ADMIN_USERNAME` | 单管理员用户名 |
| `DEVOPS_ADMIN_INITIAL_PASSWORD` | 首次启动引导密码，至少 12 字符；首次登录后删除 |
| `DEVOPS_INTERNAL_TOKEN` | API 与 Runner 内部认证 Token，至少 32 字符且两端完全一致 |
| `DEVOPS_RUNNER_INTERNAL_URL` | API 访问 Runner 内部接口的地址 |
| `DEVOPS_RUNNER_LEASE_SECONDS` | 任务租约时长 |
| `DEVOPS_METRICS_INTERVAL_SECONDS` | 主机指标采集周期，默认 30 秒 |
| `DEVOPS_RUN_LOG_RETENTION_DAYS` | 流水线日志保留天数，默认 30 天 |
| `DEVOPS_AUDIT_RETENTION_DAYS` | 审计记录保留天数，默认 180 天 |
| `DEVOPS_CORS_ORIGINS` | 允许访问 API 的浏览器 Origin 列表 |
| `DEVOPS_MCP_ALLOWED_HOSTS` | MCP 允许的 Host 列表 |
| `DEVOPS_MCP_ALLOWED_ORIGINS` | MCP 允许的浏览器 Origin 列表 |
| `DEVOPS_FRONTEND_DIR` | API 托管的前端 `dist` 目录 |
| `DEVOPS_TEMPLATE_DIR` | API 读取 Java、Node.js、Python 和 Compose 模板的目录 |
| `DEVOPS_API_PREFIX` | REST API 前缀，默认 `/api/v1` |
| `DEVOPS_ACCESS_TOKEN_MINUTES` | Web 登录访问令牌有效期，默认 30 分钟 |
| `DEVOPS_MCP_ENABLED` | 是否挂载 `/mcp`，默认启用 |
| `DEVOPS_SSE_POLL_INTERVAL_SECONDS` | SSE 查询新日志的轮询间隔 |
| `DEVOPS_SSE_BATCH_SIZE` | SSE 单次读取事件上限 |
| `DEVOPS_RUNNER_ID` | Runner 实例标识；未设置时自动生成 |
| `DEVOPS_RUNNER_POLL_SECONDS` | Runner 空闲轮询间隔，默认 1 秒 |
| `DEVOPS_RUNNER_HEARTBEAT_SECONDS` | 任务租约心跳间隔，必须短于租约 |
| `DEVOPS_INTERNAL_HOST` / `DEVOPS_INTERNAL_PORT` | Runner 内部 API 监听地址和端口；Compose/systemd 已设置安全默认值 |
| `DEVOPS_LOG_LEVEL` | Runner 日志级别，默认 `INFO` |
| `DEVOPS_HTTP_PORT` | Docker Compose 暴露的 API 主机端口，默认 `8000` |
| `DEVOPS_SSH_DIR` | Docker Compose 挂载到 Runner `/var/lib/devops/ssh` 的宿主机目录；当前核心 SSH 凭据和指纹仍由数据库管理 |
| `VITE_API_BASE_URL` | 前端 REST API 基础路径，默认 `/api/v1` |
| `VITE_DEV_API_TARGET` | Vite 开发代理目标 |
| `VITE_BUILD_SOURCEMAP` | 是否生成生产 sourcemap，默认关闭 |

列表类型配置支持 JSON 数组或逗号分隔字符串。不要把应用密码、Token、私钥放入项目 `env_config`；这些快照会写入数据库，应使用平台凭据或目标机外部 Secret 文件。

## 首次配置顺序

1. 登录并立即修改管理员初始密码。
2. 创建 Git、SSH、Registry 或 Webhook 凭据；接口永远不会返回凭据明文。
3. 登记目标服务器，扫描候选 SSH 主机密钥，并通过独立渠道核对后再保存。
4. 创建项目，选择仓库、分支、Dockerfile 来源和 Registry 凭据。
5. 在项目下创建环境，绑定服务器、唯一 `deploy_path`、Compose 来源和健康检查。
6. 配置 Git Provider Webhook 或手动触发流水线。
7. 查看构建 digest、部署 revision、健康检查、日志和审计记录。

同一服务器上的 `deploy_path` 必须全局唯一。升级到 `0005_environment_deploy_target.py` 前若已有冲突数据，迁移会失败；先把冲突环境迁移到不同目录，再重新执行升级。

## API 与实时接口

| 路径 | 作用 |
|---|---|
| `/api/v1` | REST API |
| `/api/v1/runs/{run_id}/events` | 带 Bearer Token、支持 `Last-Event-ID` 的 SSE 日志 |
| `/api/v1/ssh/sessions/{session_id}` | 一次性 SSH WebSocket 会话 |
| `/webhooks/gitlab` | GitLab Webhook |
| `/webhooks/github` | GitHub Webhook |
| `/webhooks/gitee` | Gitee Webhook |
| `/mcp` | Streamable HTTP MCP |

Webhook 分别执行验签、事件过滤、分支过滤和 delivery 去重。MCP 写工具只创建待审批操作申请，不提供任意 SSH、原始 Docker、凭据读取或直接卷删除。

## 数据、备份与恢复

最小可恢复备份必须同时包含：

1. SQLite 数据库文件。
2. `DEVOPS_SECRET_KEY_PATH` 指向的主密钥。

数据库和主密钥必须分开保存。只丢数据库会丢失平台状态；只丢主密钥会导致所有加密凭据永久无法解密；两者同时泄露等价于凭据泄露。

建议备份前停止 API 和 Runner，避免在复制 SQLite 文件时获得不一致快照。流水线工作区可以重新 checkout，通常不作为关键备份；流水线日志是否备份取决于审计要求。

恢复时先还原数据库和原主密钥，再执行：

```bash
uv --directory backend run alembic upgrade head
```

不要为旧数据库生成一把新主密钥凑数，那样数据库能打开，凭据照样全部报废。

## 升级

1. 停止 API 和 Runner。
2. 备份数据库、主密钥和需要保留的日志。
3. 更新代码与锁文件。
4. 源码部署执行 `uv --directory backend sync --python 3.13 --all-extras --dev --frozen`；systemd 或 Docker 部署按对应安装流程重新构建。
5. 执行 `uv --directory backend run alembic upgrade head`。
6. 启动 API 和 Runner，检查 `/health`、Runner 日志和最近任务状态。

Docker 镜像启动和 systemd API unit 都会在 API 启动前执行 Alembic 迁移，但生产升级仍应先做备份。

## 开发验证

后端：

```bash
uv --directory backend run ruff check .        # 代码规范检查
uv --directory backend run pyright              # 静态类型检查
uv --directory backend run pytest               # 单元测试
uv --directory backend run alembic check        # 校验数据库迁移状态一致性
```

前端：

```bash
corepack pnpm --filter devops-console-web format:check  # 检查代码格式
corepack pnpm --filter devops-console-web typecheck     # vue-tsc 类型检查
corepack pnpm --filter devops-console-web test          # Vitest 单元测试
corepack pnpm --filter devops-console-web build         # 生产构建
```

发布配置：

```bash
bash -n infra/systemd/install.sh                   # 语法检查 systemd 安装脚本
docker compose --env-file .env config --quiet      # 校验 Compose 配置
```

CI 还会在 PostgreSQL 17 上执行迁移和仓储契约测试，并构建 API、Runner 两个发布镜像。

## 常见故障

- **API 正常但 SSH/Docker 页面不可用**：检查 API 与 Runner 的 `DEVOPS_INTERNAL_TOKEN` 是否完全一致，并确认 Runner 内部接口已经启动。
- **流水线一直排队**：检查 Runner 是否运行、数据库是否为本机磁盘、任务租约是否过期，以及同项目/环境是否已有活动任务。
- **SSH 主机密钥变化**：平台会拒绝连接。必须通过云控制台或机房记录重新核对，禁止关闭指纹校验硬闯。
- **部署后健康检查失败**：检查 Compose healthcheck、目标机的 `curl`/Netcat、镜像 digest 和上一 revision 回滚结果。
- **页面只有 API 没有 UI**：确认 `frontend/dist/index.html` 存在，或检查 `DEVOPS_FRONTEND_DIR`。
- **迁移在 `0005_environment_deploy_target.py` 失败**：同一服务器存在重复 `deploy_path`，先修正冲突环境。

## 安全与范围边界

- 自定义 Dockerfile、SSH 脚本和 Docker 管理都等价于高权限代码执行。
- v1 只允许可信管理员、可信仓库、可信 Dockerfile 和可信脚本，不提供不可信代码沙箱。
- 不实现 Kubernetes、多租户、分布式 Runner、任意 DAG、蓝绿发布、插件市场和长期时序数据库。
- SSH 终端只记录登录人、服务器、来源和时间，不保存完整终端录像。
- 危险 Docker 删除必须先展示依赖影响并要求输入目标名称确认。

## OpenAI MCP 与 Skills

`/mcp` 暴露 Streamable HTTP MCP。`skills/devops-observer` 仅用于查询与诊断，`skills/devops-operator` 只提交需要 Web 审批的操作申请。实现方式与 [OpenAI Docs 的 MCP server quickstart](https://developers.openai.com/plugins/build/app-quickstart.md) 描述的 `/mcp` server-backed capabilities 模式一致。

安装到 Codex Skill 目录：

```bash
codex_skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
install -d "$codex_skill_root"
cp -R skills/devops-observer skills/devops-operator "$codex_skill_root/"
```

两个 `agents/openai.yaml` 中的 `https://devops.local/mcp` 只是示例。必须替换为 MCP 客户端真正可访问的 TLS 或局域网地址，并同步配置 `DEVOPS_MCP_ALLOWED_HOSTS`、`DEVOPS_MCP_ALLOWED_ORIGINS` 和反向代理。

在 Web 控制台“通知与 MCP”中签发短期、最小权限 Token，并通过 `Authorization: Bearer <token>` 连接。Token 只显示一次，不要写入 Skill、Git 仓库或共享配置。
