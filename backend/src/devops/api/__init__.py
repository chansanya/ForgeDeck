"""导出 FastAPI 应用实例与测试可复用的应用工厂。"""

from devops.api.main import app, create_app

__all__ = ["app", "create_app"]
