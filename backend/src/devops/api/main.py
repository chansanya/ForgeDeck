"""组装 FastAPI 控制面、生命周期、中间件、MCP 与静态前端入口。

API 只处理认证、配置和持久状态，不直接获得 Docker Socket 权限。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from starlette.routing import Route

from devops.config import Settings, get_settings
from devops.db import Database
from devops.domain.models import AdminUser
from devops.logging_config import configure_logging
from devops.security import SecretManager, hash_password

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """组装 API 路由、中间件和生命周期资源，保持 Docker 权限隔离。"""
    resolved_settings = settings or get_settings()
    configure_logging(development=resolved_settings.environment == "development")
    database = Database(resolved_settings.database_url)
    active_secret_manager: SecretManager | None = None

    def current_secret_manager() -> SecretManager | None:
        """从应用状态读取已初始化的主密钥管理器。"""
        return active_secret_manager

    mcp_server = None
    if resolved_settings.mcp_enabled:
        from devops.integrations.mcp import create_mcp_server

        mcp_server = create_mcp_server(
            database,
            runner_internal_url=resolved_settings.runner_internal_url,
            internal_token=resolved_settings.internal_token,
            secret_manager_provider=current_secret_manager,
            allowed_hosts=resolved_settings.mcp_allowed_hosts,
            allowed_origins=resolved_settings.mcp_allowed_origins,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """初始化数据库和后台资源，并在应用退出时释放连接。"""
        nonlocal active_secret_manager
        resolved_settings.data_dir.mkdir(parents=True, exist_ok=True)
        active_secret_manager = SecretManager.from_key_file(resolved_settings.secret_key_path)
        app.state.secret_manager = active_secret_manager
        if resolved_settings.auto_create_schema:
            await database.create_schema()
        await _bootstrap_admin(database, resolved_settings)
        try:
            async with AsyncExitStack() as stack:
                if mcp_server is not None:
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            active_secret_manager = None
            await database.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "MCP-Protocol-Version",
            "Mcp-Session-Id",
            "X-Request-ID",
        ],
        expose_headers=["Mcp-Session-Id", "X-Request-ID"],
    )

    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """为请求生成或透传 trace id，便于跨 API、Runner 和审计日志关联。"""
        request.state.trace_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(trace_id=request.state.trace_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.trace_id
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return response
        except Exception:
            logger.exception(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """返回轻量存活探针，不执行数据库、Docker 或 SSH 操作。"""
        return {"status": "ok"}

    from devops.api.routes import api_router, webhook_router

    application.include_router(api_router, prefix=resolved_settings.api_prefix)
    application.include_router(webhook_router, prefix="/webhooks")
    if mcp_server is not None:
        from devops.integrations.mcp import BearerAuthMiddleware, MCPPathAlias

        mcp_app = MCPPathAlias(
            BearerAuthMiddleware(
                mcp_server.streamable_http_app(),
                database,
                bootstrap_token=resolved_settings.mcp_token,
            )
        )
        methods = ["GET", "POST", "DELETE", "OPTIONS"]
        application.router.routes.append(Route("/mcp", mcp_app, methods=methods, name="mcp"))
        application.router.routes.append(Route("/mcp/", mcp_app, methods=methods))

        @application.api_route(
            "/mcp/{path:path}",
            methods=methods,
            include_in_schema=False,
        )
        async def unknown_mcp_path(path: str) -> None:
            """拒绝非规范 MCP 路径，避免客户端绕过统一认证入口。"""
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown MCP path: {path}",
            )

    @application.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @application.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def oauth_discovery_not_configured() -> None:
        """明确返回 OAuth 发现未配置，避免误把 MCP 当作 OAuth 服务。"""
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OAuth is not configured")
    from devops.api.static import mount_frontend

    mount_frontend(application, resolved_settings.frontend_dir)
    return application


async def _bootstrap_admin(database: Database, settings: Settings) -> None:
    """首次启动创建管理员；已有账户不会被环境变量密码覆盖。"""
    async with database.session_factory() as session:
        existing = await session.scalar(select(AdminUser.id).limit(1))
        if existing:
            return
        if not settings.admin_initial_password:
            logger.warning("administrator_missing", remediation="python -m devops.cli init-admin")
            return
        session.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_initial_password),
            )
        )
        await session.commit()
        logger.warning("initial_administrator_created", username=settings.admin_username)


app = create_app()
