# Light DevOps 后端

后端仅支持 Python 3.13。FastAPI API 和 Runner 是共享 SQLite 持久任务队列的独立进程：API 负责认证、配置、审批和审计，Runner 才能访问 Docker 与 SSH。

完整目录职责见[目录与模块说明](../docs/project-structure.md)。
文件说明、关键逻辑注释和验证规则见[Agent 开发规范](../AGENTS.md)。
测试用例说明见[后端测试说明](tests/README.md)。

## 本地开发

从仓库根目录执行：

```bash
uv python install 3.13
cp backend/.env.example backend/.env
uv --directory backend sync --python 3.13 --all-extras --dev --frozen
uv --directory backend run alembic upgrade head
uv --directory backend run python -m devops.cli init-admin
```

`uv` 是 Rust 实现的 Python 包管理器，替代 pip + venv + pip-tools。`--directory backend` 指定项目根目录为 `backend/`，无需先 `cd`。各参数含义：

| 命令 | 说明 |
|---|---|
| `uv python install 3.13` | 下载安装 Python 3.13 解释器 |
| `sync` | 根据 `pyproject.toml` 和 `uv.lock` 同步虚拟环境依赖 |
| `--python 3.13` | 指定使用 Python 3.13 |
| `--all-extras` | 安装所有可选依赖组（`postgres`、`runner`） |
| `--dev` | 同时安装开发依赖（pytest、ruff、pyright 等） |
| `--frozen` | 严格按 `uv.lock` 安装，不更新锁文件 |
| `run alembic upgrade head` | 在虚拟环境中执行数据库迁移到最新版本 |
| `run python -m devops.cli init-admin` | 创建初始管理员账号 |

分别启动 API 和 Runner：

```bash
# 开发模式（带热重载，改代码自动重启）
uv --directory backend run uvicorn devops.api.main:app --host 0.0.0.0 --port 8000 --reload
```

```bash
uv --directory backend run python -m devops.runner.main
```

| 命令 | 说明 |
|---|---|
| `uvicorn ... --reload` | 启动 API 进程，监听 8000 端口；`--reload` 仅开发用，改代码自动重启 |
| `python -m devops.runner.main` | 启动 Runner 进程，轮询任务队列并执行构建、部署、脚本和指标采集 |

`pyproject.toml` 的 `[project.scripts]` 还注册了两个快捷命令，参数固定不可调，适合生产或常规启动：

```bash
uv --directory backend run devops-api      # 等价于 uvicorn devops.api.main:app（host=0.0.0.0 port=8000 无热重载）
uv --directory backend run devops-runner   # 等价于 python -m devops.runner.main
```

| 快捷命令 | 对应入口 | 固定参数 | 适用场景 |
|---|---|---|---|
| `devops-api` | `devops.main:run_api` | host=0.0.0.0、port=8000、reload=False | 生产 / 常规启动 |
| `devops-runner` | `devops.runner.main:run_runner` | 无额外参数 | 生产 / 常规启动 |

两种方式都读同一个 `backend/.env`，区别仅在于开发模式能自定义 host、port 和热重载。`backend/.env` 与根目录 Docker Compose 使用的 `.env` 不是同一个文件。API 与 Runner 必须读取完全相同、至少 32 字符的 `DEVOPS_INTERNAL_TOKEN`，否则 Runner 内部 API、SSH 终端和 Docker 管理代理不可用。

## 入口

- `/health`：健康检查
- `/docs`：OpenAPI
- `/api/v1`：业务 REST API
- `/webhooks/{gitlab,github,gitee}`：Git Provider Webhook
- `/api/v1/runs/{run_id}/events`：SSE 流水线日志
- `/api/v1/ssh/sessions/{session_id}`：SSH WebSocket
- `/mcp`：Streamable HTTP MCP

## 数据与权限

- SQLite 必须位于本机文件系统，禁止放 NFS。
- API 进程不得获得 `/var/run/docker.sock`。
- 主密钥默认位于 `secrets/master.key`，必须与 SQLite 分开备份。
- 丢失主密钥后，加密凭据不可恢复。
- 同一服务器的环境部署目录必须唯一；迁移 `0005_environment_deploy_target.py` 会拒绝重复 `(server_id, deploy_path)` 数据。

## 验证

```bash
uv --directory backend run ruff check .
uv --directory backend run pyright
uv --directory backend run pytest
uv --directory backend run alembic check
```

| 命令 | 说明 |
|---|---|
| `ruff check .` | 代码规范检查（lint），检查 import 顺序、未使用变量、语法错误等 |
| `pyright` | 静态类型检查，捕获类型不匹配、空值和属性错误 |
| `pytest` | 单元测试，覆盖认证、API、Webhook、SSE、MCP、迁移和 Runner 全链路 |
| `alembic check` | 校验当前数据库状态与迁移链是否一致，有未执行迁移会报错 |

可选 PostgreSQL 仓储契约通过 `DEVOPS_POSTGRES_TEST_URL` 启用；v1 不承诺 PostgreSQL 生产支持。

## 生产前置条件

systemd 安装器要求 Linux amd64/x86_64、uv 0.11.16+、Python 3.13、Node.js 24、Corepack、pnpm 10.14.0、Docker Engine、Buildx、Compose V2 2.20.0+ 和预先存在的 `docker` 用户组。

目标服务器需要 Docker Engine、Compose V2 2.20.0+ 和部署 SSH 用户的 Docker 权限。HTTP 健康检查需要 `curl`，TCP 健康检查需要兼容 `nc -z -w` 的 Netcat。

## MCP Skills

`skills/devops-observer` 只允许查询，`skills/devops-operator` 只允许创建待 Web 审批的操作申请。MCP Token 应短期、最小权限签发，不能写入 Skill 目录或仓库。
