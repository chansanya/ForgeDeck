"""创建异步数据库引擎与 Session，并为 SQLite 设置并发和完整性参数。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from devops.domain.models import Base


class _DBAPICursor(Protocol):
    def execute(self, statement: str) -> object: ...

    def close(self) -> None: ...


class _DBAPIConnection(Protocol):
    def cursor(self) -> _DBAPICursor: ...


class Database:
    def __init__(self, url: str) -> None:
        """根据数据库 URL 创建异步引擎，并为 SQLite 配置并发安全参数。"""
        self.url = url
        self._ensure_sqlite_parent(url)
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={"timeout": 30} if url.startswith("sqlite") else {},
        )
        if self.engine.dialect.name == "sqlite":
            self._configure_sqlite(self.engine)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database:
            return
        if parsed.database == ":memory:" or parsed.database.startswith("file:"):
            return
        Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _configure_sqlite(engine: AsyncEngine) -> None:
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection: _DBAPIConnection, _: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # 每条 SQLite 连接都要重复设置：外键和 busy_timeout 是连接级参数，
                # WAL + 短等待用于缓解 API 与 Runner 双进程争用，但不能替代短事务。
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    async def create_schema(self) -> None:
        """在开发/测试模式创建当前模型表；生产升级由 Alembic 负责。"""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        """删除当前模型表，仅供显式授权的测试清理流程使用。"""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        """返回一个短生命周期数据库会话迭代器。"""
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        """释放数据库连接池。"""
        await self.engine.dispose()
