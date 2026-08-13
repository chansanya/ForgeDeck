# Light DevOps Agent 开发规范

本文件适用于仓库根目录及全部子目录。自动化 Agent、开发者和代码审查者修改项目时必须遵守。

## 沟通与工作方式

- 默认使用简体中文沟通；代码标识符、命令、日志和报错保持原始语言。
- 修改前先使用 `rg`、`rg --files` 定位相关源码、配置、测试和文档，确认真实调用链后再动手。
- 仅修改当前需求涉及的代码，不顺手重构无关模块，不覆盖用户已有改动。
- 发现需求与现有安全边界、状态机或部署语义冲突时，必须先指出风险，不能靠猜测硬改。
- 文件编辑使用补丁方式完成；禁止修改依赖目录、构建产物、虚拟环境和运行时业务数据。

## 核心代码范围

下列内容属于核心代码或核心运行配置：

- `backend/src/devops/`：API、领域模型、数据库、集成和 Runner 生产代码。
- `backend/alembic/`：数据库迁移环境与版本升级链。
- `frontend/src/`：Vue 页面、组件、状态、路由、API 客户端、类型和样式。
- `frontend/vite.config.ts`：前端开发代理与生产构建配置。
- `docker-compose.yml`、`infra/`：容器镜像、Compose 和 systemd 发布链路。
- `templates/`：提供给项目使用的 Dockerfile 与 Compose 模板。

测试、文档和示例也要保持可读，但生成物、依赖与缓存不属于人工注释范围，包括 `dist/`、`node_modules/`、`.venv*/`、`__pycache__/` 和工具缓存。

## 文件头功能说明

- 新增或修改核心代码文件时，文件开头必须有简短功能说明，写清“负责什么、位于哪一层、关键边界是什么”。
- 说明控制在 1～3 句，不写作者、日期、修改历史，不复述文件名，不维护容易过期的函数清单。
- Python 使用模块 docstring，必须位于 `from __future__ import ...` 之前。
- Vue SFC 使用文件首部 HTML 注释，随后仍保持 `template → script setup → style` 顺序。
- TypeScript 和 CSS 使用文件首部块注释。`.d.ts` 如依赖 triple-slash directive，可先保留 directive，再紧跟功能说明。
- Shell 的 shebang 必须保持第一行，功能说明从第二行开始；Dockerfile、YAML、TOML 和 `.env.example` 使用该格式支持的注释语法。
- 严格 JSON 不允许注释。使用已有 `description` 等语义字段说明用途，或在相邻 README/文档中说明，禁止为了满足形式要求生成非法 JSON。
- Alembic 迁移文件的说明必须描述升级目的和不可逆/兼容性约束，不能删除历史迁移来“整理目录”。

推荐示例：

```python
"""管理 Runner 持久任务租约与 CAS 状态更新。

本模块只负责任务所有权和状态持久化，不执行 Docker、SSH 等外部副作用。
"""
```

```vue
<!-- 服务器管理页：登记目标主机、确认 SSH 指纹并展示最近指标。 -->
<template>
```

## 关键逻辑注释

- 对任务租约、CAS 更新、并发锁、幂等恢复、配置快照、参数哈希、Webhook 验签、SSH 指纹、目录穿越防护、日志脱敏、部署对账和回滚等逻辑，必须说明设计原因和必须保持的不变量。
- 注释重点解释“为什么这样做”“失败后如何保证一致性”“删除这段保护会造成什么后果”。
- 禁止写无信息量注释，例如“遍历列表”“调用函数”“返回结果”。代码已经说清楚的内容不要再翻译一遍。
- 代码变化导致注释失真时，必须在同一个改动中更新或删除注释；错误注释比没有注释更危险。
- 安全限制、事务边界和状态机约束不能只写在注释里，必须同时由类型、校验、数据库约束或测试执行。

## 函数注释规范

- 生产代码中的公开函数、跨模块调用函数，以及涉及状态迁移、并发、幂等、安全校验、外部命令、Docker、SSH、数据库或网络请求的函数，必须在函数体第一条语句使用中文 docstring（Python）或中文 JSDoc（TypeScript/Vue）。
- 函数说明至少交代“职责 + 关键边界/副作用”；参数和返回值只有在语义不直观或存在安全约束时才补充，禁止为每个参数机械写注释。
- 私有的纯转换、简单属性访问和协议占位函数不强制添加冗余说明；但只要包含业务判断、错误处理或安全保护，就必须说明设计原因和失败语义。
- Python docstring 使用完整中文句子；TypeScript/Vue 在声明前使用 `/** ... */`，回调函数可用紧邻的中文行注释说明其事件语义。
- 测试辅助函数按测试意图添加中文说明，测试断言本身已经清楚表达意图时不重复解释实现细节。
- 不得用统一的“处理数据”“执行函数”“返回结果”等空泛句式充数；函数重命名或行为变化时，同步更新注释。

## 后端与 Runner 约束

- API 与 Runner 保持权限隔离：API 不访问 Docker Socket，Docker、SSH 和部署副作用只能在 Runner 执行。
- SQLite 使用 WAL、外键、`busy_timeout`、短事务和 CAS；禁止用进程内队列替代持久任务状态。
- Git、Buildx、Compose 和脚本进程必须使用参数数组执行，禁止 `shell=True` 和字符串拼接命令。
- SSH 必须校验已登记主机指纹；不得通过关闭 `known_hosts` 校验绕过主机密钥变化。
- 所有凭据、Token、环境变量和日志输出都要经过最小暴露与脱敏检查，接口不得返回明文凭据。
- 新状态、新字段或新任务类型必须同步检查模型、Schema、迁移、仓储、Runner handler、前端类型和测试。

## 前端约束

- 使用 Vue 3、TypeScript、Naive UI 和 Lucide；禁止使用表情符号充当图标。
- Vue 文件块顺序固定为 `template → script setup lang="ts" → style`。
- CSS 必须保持多行格式，不允许把整条规则压成一行；统一交给 Prettier 格式化。
- 页面只通过 REST、SSE 和 WebSocket 调用 API，不读取数据库，不在浏览器保存服务端凭据明文。
- 新业务页面放 `views/`，复用组件放 `components/`，传输类型放 `api/types.ts`，请求封装放 `api/client.ts`。

## 文档与目录

- 新增、删除或调整目录/核心模块职责时，同步更新 `docs/project-structure.md`。
- 启动方式、环境变量、部署路径、备份恢复或安全边界变化时，同步更新根 `README.md` 和对应子项目 README。
- 文档必须描述当前实现，不能把规划中的能力写成已经完成。

## 验证要求

完成修改后，至少执行与改动匹配的检查：

```bash
uv --directory backend run --frozen --no-sync python ../scripts/check_file_headers.py
uv --directory backend run ruff check .
uv --directory backend run pyright
uv --directory backend run pytest
corepack pnpm --filter devops-console-web format:check
corepack pnpm --filter devops-console-web typecheck
corepack pnpm --filter devops-console-web test
corepack pnpm --filter devops-console-web build
bash -n infra/systemd/install.sh
```

- 只改文档可缩小验证范围，但必须检查 UTF-8、Markdown 链接和文档中的路径/命令。
- 修改核心代码必须验证文件头说明仍存在，并检查关键注释没有因实现变化而失真。
- 不得用“测试没跑”冒充“测试通过”；无法执行的检查要明确说明原因。
