"""按 SPA fallback 规则托管已构建的 Vue 静态文件。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        """资源不存在时回退到 index.html，同时保留非 404 错误。"""
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def mount_frontend(app: FastAPI, directory: Path | None) -> None:
    """挂载已构建的 Vue 静态目录；目录缺失时保持 API-only 模式。"""
    if directory is None:
        return
    resolved = directory.expanduser().resolve()
    if not (resolved / "index.html").is_file():
        return
    app.mount("/", SPAStaticFiles(directory=resolved, html=True), name="frontend")
