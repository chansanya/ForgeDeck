"""封装领域对象访问以及 Runner 持久任务的租约与 CAS 更新。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from devops.db.results import affected_rows
from devops.domain.models import Base, RunnerTask, TaskKind, TaskState, utcnow


class GenericRepository[ModelT: Base]:
    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    async def get(self, entity_id: Any) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[ModelT]:
        query: Select[tuple[ModelT]] = select(self.model).offset(offset).limit(limit)
        return list((await self.session.scalars(query)).all())


class RunnerTaskRepository(GenericRepository[RunnerTask]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RunnerTask)

    async def lease_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        kinds: Iterable[TaskKind] | None = None,
    ) -> RunnerTask | None:
        """Lease one runnable task using optimistic CAS.

        SQLite does not provide useful row-level `FOR UPDATE`, so candidates are read in
        priority order and claimed with a version/state guarded UPDATE. PostgreSQL uses the
        exact same contract; a future adapter may add SKIP LOCKED without changing Runner.
        """

        now = utcnow()
        expired = and_(
            RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
            RunnerTask.lease_expires_at.is_not(None),
            RunnerTask.lease_expires_at < now,
        )
        runnable = or_(
            and_(RunnerTask.state == TaskState.PENDING, RunnerTask.available_at <= now),
            expired,
        )
        query = select(RunnerTask).where(runnable, RunnerTask.attempts < RunnerTask.max_attempts)
        if kinds:
            query = query.where(RunnerTask.kind.in_(list(kinds)))
        query = query.order_by(RunnerTask.priority.asc(), RunnerTask.created_at.asc()).limit(20)
        candidates = list((await self.session.scalars(query)).all())

        for candidate in candidates:
            # 同一 resource_key 只能有一个有效租约；version/state 条件让多个
            # Runner 即使同时读到候选项，也只有一个 UPDATE 能成功获得所有权。
            active_same_resource = exists(
                select(RunnerTask.id).where(
                    RunnerTask.id != candidate.id,
                    RunnerTask.resource_key == candidate.resource_key,
                    RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
                    RunnerTask.lease_expires_at > now,
                )
            )
            claim = (
                update(RunnerTask)
                .where(
                    RunnerTask.id == candidate.id,
                    RunnerTask.version == candidate.version,
                    runnable,
                    ~active_same_resource,
                )
                .values(
                    state=TaskState.LEASED,
                    leased_by=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    attempts=RunnerTask.attempts + 1,
                    version=RunnerTask.version + 1,
                    error_message=None,
                )
            )
            result = await self.session.execute(claim)
            if affected_rows(result) == 1:
                await self.session.flush()
                return await self.session.get(RunnerTask, candidate.id, populate_existing=True)
        return None

    async def mark_running(self, task_id: str, worker_id: str) -> bool:
        result = await self.session.execute(
            update(RunnerTask)
            .where(
                RunnerTask.id == task_id,
                RunnerTask.leased_by == worker_id,
                RunnerTask.state == TaskState.LEASED,
            )
            .values(state=TaskState.RUNNING, version=RunnerTask.version + 1)
        )
        return affected_rows(result) == 1

    async def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = utcnow()
        result = await self.session.execute(
            update(RunnerTask)
            .where(
                RunnerTask.id == task_id,
                RunnerTask.leased_by == worker_id,
                RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                version=RunnerTask.version + 1,
            )
        )
        return affected_rows(result) == 1

    async def complete(
        self,
        task_id: str,
        worker_id: str,
        *,
        succeeded: bool,
        error_message: str | None = None,
    ) -> bool:
        result = await self.session.execute(
            update(RunnerTask)
            .where(
                RunnerTask.id == task_id,
                RunnerTask.leased_by == worker_id,
                RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
            )
            .values(
                state=TaskState.SUCCEEDED if succeeded else TaskState.FAILED,
                lease_expires_at=None,
                heartbeat_at=utcnow(),
                error_message=error_message,
                version=RunnerTask.version + 1,
            )
        )
        return affected_rows(result) == 1

    async def cancel(self, task_id: str, worker_id: str) -> bool:
        result = await self.session.execute(
            update(RunnerTask)
            .where(
                RunnerTask.id == task_id,
                RunnerTask.leased_by == worker_id,
                RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
            )
            .values(
                state=TaskState.CANCELLED,
                lease_expires_at=None,
                heartbeat_at=utcnow(),
                version=RunnerTask.version + 1,
            )
        )
        return affected_rows(result) == 1

    async def retry(
        self,
        task_id: str,
        worker_id: str,
        *,
        available_at: datetime,
        error_message: str,
    ) -> bool:
        result = await self.session.execute(
            update(RunnerTask)
            .where(
                RunnerTask.id == task_id,
                RunnerTask.leased_by == worker_id,
                RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
                RunnerTask.attempts < RunnerTask.max_attempts,
            )
            .values(
                state=TaskState.PENDING,
                available_at=available_at,
                leased_by=None,
                lease_expires_at=None,
                heartbeat_at=utcnow(),
                error_message=error_message,
                version=RunnerTask.version + 1,
            )
        )
        return affected_rows(result) == 1

    async def recover_expired(self, now: datetime | None = None) -> int:
        resolved_now = now or utcnow()
        expired_filter = and_(
            RunnerTask.state.in_([TaskState.LEASED, TaskState.RUNNING]),
            RunnerTask.lease_expires_at.is_not(None),
            RunnerTask.lease_expires_at < resolved_now,
        )
        exhausted = await self.session.execute(
            update(RunnerTask)
            .where(expired_filter, RunnerTask.attempts >= RunnerTask.max_attempts)
            .values(
                state=TaskState.FAILED,
                leased_by=None,
                lease_expires_at=None,
                error_message="Lease expired and retry limit was reached",
                version=RunnerTask.version + 1,
            )
        )
        retryable = await self.session.execute(
            update(RunnerTask)
            .where(expired_filter, RunnerTask.attempts < RunnerTask.max_attempts)
            .values(
                state=TaskState.PENDING,
                available_at=resolved_now,
                leased_by=None,
                lease_expires_at=None,
                error_message="Lease expired; task recovered",
                version=RunnerTask.version + 1,
            )
        )
        return affected_rows(exhausted) + affected_rows(retryable)

    async def cancellation_requested(self, task: RunnerTask) -> bool:
        if task.state == TaskState.CANCELLED:
            return True
        if not task.run_id:
            return False
        from devops.domain.models import PipelineRun

        value = await self.session.scalar(
            select(PipelineRun.cancel_requested).where(PipelineRun.id == task.run_id)
        )
        return bool(value)
