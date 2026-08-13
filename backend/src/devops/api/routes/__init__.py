"""汇总并注册 `/api/v1` 业务路由和 Git Provider Webhook 路由。"""

from fastapi import APIRouter

from devops.api.routes import (
    approvals,
    audit,
    auth,
    credentials,
    dashboard,
    deployments,
    mcp_tokens,
    notifications,
    pipelines,
    projects,
    runner_proxy,
    scripts,
    servers,
    templates,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(credentials.router)
api_router.include_router(projects.router)
api_router.include_router(servers.router)
api_router.include_router(templates.router)
api_router.include_router(pipelines.router)
api_router.include_router(deployments.router)
api_router.include_router(scripts.router)
api_router.include_router(approvals.router)
api_router.include_router(audit.router)
api_router.include_router(notifications.router)
api_router.include_router(mcp_tokens.router)
api_router.include_router(runner_proxy.router)
api_router.include_router(runner_proxy.ssh_router)

webhook_router = webhooks.router

__all__ = ["api_router", "webhook_router"]
