"""周期创建主机指标任务并清理过期流水线日志和审计元数据。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devops.domain.models import (
    AuditEvent,
    HostMetric,
    RunLog,
    RunnerTask,
    Server,
    TaskKind,
    TaskState,
    utcnow,
)

logger = logging.getLogger(__name__)


class MaintenanceScheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: float = 30,
        metric_retention_hours: int = 24,
        log_retention_days: int = 30,
        audit_retention_days: int = 180,
        log_dir: Path | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds
        self._metric_retention_hours = metric_retention_hours
        self._log_retention_days = log_retention_days
        self._audit_retention_days = audit_retention_days
        self._log_dir = log_dir

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("runner maintenance tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    async def tick(self) -> int:
        now = utcnow()
        async with self._session_factory() as session:
            servers = list(
                (
                    await session.scalars(
                        select(Server).where(
                            Server.enabled.is_(True),
                            Server.host_key.is_not(None),
                            Server.ssh_credential_id.is_not(None),
                        )
                    )
                ).all()
            )
            active_keys = set(
                (
                    await session.scalars(
                        select(RunnerTask.resource_key).where(
                            RunnerTask.kind == TaskKind.METRICS,
                            RunnerTask.state.in_(
                                (TaskState.PENDING, TaskState.LEASED, TaskState.RUNNING)
                            ),
                        )
                    )
                ).all()
            )
            added = 0
            for server in servers:
                resource_key = f"server:{server.id}:metrics"
                if resource_key in active_keys:
                    continue
                session.add(
                    RunnerTask(
                        kind=TaskKind.METRICS,
                        resource_key=resource_key,
                        payload={"server_id": server.id},
                        priority=500,
                        max_attempts=2,
                    )
                )
                added += 1
            await session.execute(
                delete(HostMetric).where(
                    HostMetric.collected_at
                    < now - timedelta(hours=self._metric_retention_hours)
                )
            )
            await session.execute(
                delete(RunLog).where(
                    RunLog.created_at < now - timedelta(days=self._log_retention_days)
                )
            )
            await session.execute(
                delete(AuditEvent).where(
                    AuditEvent.created_at < now - timedelta(days=self._audit_retention_days)
                )
            )
            await session.commit()
        if self._log_dir is not None:
            cutoff = (now - timedelta(days=self._log_retention_days)).timestamp()
            try:
                await asyncio.to_thread(
                    _cleanup_expired_jsonl,
                    self._log_dir / "runs",
                    cutoff,
                )
            except Exception:
                logger.exception("failed to clean expired pipeline JSONL logs")
        return added


def _cleanup_expired_jsonl(directory: Path, cutoff_timestamp: float) -> None:
    if not directory.is_dir():
        return
    for path in directory.glob("*.jsonl"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff_timestamp:
                path.unlink()
        except FileNotFoundError:
            continue
