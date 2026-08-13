# 后端测试

> 相关文档：[后端开发说明](../README.md) · [项目结构](../../docs/project-structure.md) · [Agent 开发规范](../../AGENTS.md)

所有测试使用 pytest + pytest-asyncio，`asyncio_mode = "auto"`，`testpaths = ["tests"]`（见 `pyproject.toml`）。

## 运行方式

```bash
# 从 backend/ 目录执行
uv run pytest                          # 全部测试
uv run pytest tests/                   # 同上
uv run pytest tests/test_auth_and_resources.py   # 单个文件
uv run pytest tests/runner/            # 只跑 Runner 子目录
uv run pytest -k "ssh"                 # 按名称匹配
uv run pytest -v                       # 显示每个用例名
```

## 公共 Fixture

`conftest.py` 提供三个核心 fixture，所有顶层测试共享：

| Fixture | 作用 |
|---|---|
| `app` | 用临时 SQLite + 临时主密钥创建的 FastAPI 测试应用，已走完生命周期启动 |
| `client` | 基于 `httpx.ASGITransport` 的异步 HTTP 客户端，直接打 FastAPI 不走网络 |
| `auth_headers` | 已登录管理员的 `{"Authorization": "Bearer ..."}` 请求头 |

`runner/` 子目录的测试不依赖 FastAPI，直接实例化 Runner 组件并用 mock 或临时文件验证行为。

## 顶层测试

| 文件 | 测试内容 |
|---|---|
| `conftest.py` | 公共 fixture：临时数据库、测试应用、登录 Token |
| `test_auth_and_resources.py` | 管理员登录、密码修改、项目/服务器/凭据/脚本/部署等资源 CRUD |
| `test_approvals.py` | 操作申请创建、审批通过/拒绝、参数哈希复核 |
| `test_webhooks_and_sse.py` | GitLab/GitHub/Gitee Webhook 验签、事件过滤、delivery 去重、SSE 日志流 |
| `test_mcp.py` | MCP Streamable HTTP 工具调用、Bearer Token 认证和 scope 控制 |
| `test_notifications.py` | 钉钉机器人、HMAC Webhook、SMTP 邮件通知发送 |
| `test_migrations.py` | Alembic 迁移链完整性：SQLite 全量 upgrade + downgrade 回滚 |
| `test_repository_contract.py` | 仓储契约测试，可选 PostgreSQL（`DEVOPS_POSTGRES_TEST_URL`）验证兼容性 |
| `test_task_repository.py` | 任务队列入队、领取（CAS）、心跳续约、终态提交 |

## Runner 测试（`runner/`）

| 文件 | 测试内容 |
|---|---|
| `test_engine.py` | 任务 Worker 轮询、租约获取与过期、取消和重试、崩溃恢复 |
| `test_store.py` | 持久任务队列的 CAS 状态更新、JSONL 日志索引、恢复扫描 |
| `test_state.py` | 流水线/部署/任务的合法状态迁移路径和非法迁移拒绝 |
| `test_handlers_persistence.py` | Pipeline/Deployment/Script/Metrics 四类 handler 的阶段 checkpoint 持久化 |
| `test_build.py` | Buildx 构建命令组装、`--metadata-file` digest 解析、Registry digest 回退解析 |
| `test_source.py` | Git checkout、commit 固化、路径逃逸防护（`..` 和绝对路径拒绝） |
| `test_deploy.py` | Compose 上传、pending 快照、健康检查（HTTP/TCP/compose）、对账和回滚 |
| `test_docker.py` | Docker SDK 容器/镜像/网络/卷管理、危险删除操作的影响预览 |
| `test_ssh.py` | AsyncSSH 连接、主机指纹校验、命令执行、SFTP 传输、PTY 会话 |
| `test_process.py` | 子进程参数数组执行（禁止 `shell=True`）、超时和取消 |
| `test_credentials.py` | 任务凭据解密、Git askpass 生成、临时 Docker config 创建和清理 |
| `test_metrics.py` | CPU 使用率、内存、磁盘空间和网络 IO 采集逻辑 |
| `test_logs.py` | 运行日志脱敏（凭据/Token 遮蔽）、追加写入和有界读取 |
| `test_internal_api.py` | Runner 内部 HTTP/WebSocket 接口的 Token 认证和 Docker/SSH 代理 |

## 注意事项

- 测试使用临时目录（`tmp_path`），运行完自动清理，不污染本地数据库。
- Runner 测试通过 mock 绕过真实的 Docker daemon 和 SSH 连接，不需要本机装 Docker。
- `test_repository_contract.py` 默认只跑 SQLite；设置 `DEVOPS_POSTGRES_TEST_URL` 后额外验证 PostgreSQL。
