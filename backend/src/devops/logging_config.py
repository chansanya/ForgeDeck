"""配置 API 使用的标准日志与 structlog 渲染链。"""

from __future__ import annotations

import logging

import structlog


def configure_logging(*, development: bool = False) -> None:
    """配置结构化控制台日志，并按环境选择开发可读格式。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    renderer = (
        structlog.dev.ConsoleRenderer(colors=False)
        if development
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
