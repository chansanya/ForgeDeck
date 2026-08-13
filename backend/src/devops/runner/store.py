"""实现 Runner 的 SQLAlchemy 持久队列、CAS 租约、恢复和日志索引。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devops.db.results import affected_rows
from devops.db.uow import UnitOfWork
from devops.domain.models import (
    ApprovalState,
    Deployment,
    DeploymentStatus,
    OperationRequest,
    PipelineRun,
    RunLog,
    RunnerTask,
    RunStatus,
    TaskKind,
    TaskState,
    utcnow,
)
from devops.integrations.notifications import (
    deliver_event,
    deployment_result_notification,
    run_result_notification,
)
from devops.runner.contracts import LogEvent, TaskLease
from devops.runner.state import ensure_task_transition
from devops.security import SecretManager

logger = logging.getLogger(__name__)


class SQLAlchemyRunnerTaskStore:
    """Short-transaction adapter implementing Runner's lease/CAS contract."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_manager: SecretManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_manager = secret_manager

    async def recover_expired(self, now: datetime) -> int:
        failed_run_ids: tuple[str, ...] = ()
        failed_deployment_ids: tuple[str, ...] = ()
        recovered_message = "runner lease expired; task recovered"
        exhausted_message = "runner lease expired and retry limit was exhausted"
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            expired = and_(
                RunnerTask.state.in_((TaskState.LEASED, TaskState.RUNNING)),
                RunnerTask.lease_expires_at.is_not(None),
                RunnerTask.lease_expires_at <= now,
            )
            retryable = and_(expired, RunnerTask.attempts < RunnerTask.max_attempts)
            exhausted = and_(expired, RunnerTask.attempts >= RunnerTask.max_attempts)
            retry_result = await uow.session.execute(
                update(RunnerTask)
                .where(retryable)
                .values(
                    state=TaskState.PENDING,
                    available_at=now,
                    leased_by=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    error_message=recovered_message,
                    version=RunnerTask.version + 1,
                )
                .returning(
                    RunnerTask.run_id,
                    RunnerTask.deployment_id,
                    RunnerTask.operation_id,
                )
            )
            retry_links = retry_result.all()
            retry_run_ids = tuple(
                row.run_id for row in retry_links if row.run_id is not None
            )
            retry_deployment_ids = tuple(
                row.deployment_id
                for row in retry_links
                if row.deployment_id is not None
            )
            retry_operation_ids = tuple(
                row.operation_id for row in retry_links if row.operation_id is not None
            )
            failed_result = await uow.session.execute(
                update(RunnerTask)
                .where(exhausted)
                .values(
                    state=TaskState.FAILED,
                    leased_by=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    error_message=exhausted_message,
                    version=RunnerTask.version + 1,
                )
                .returning(
                    RunnerTask.run_id,
                    RunnerTask.deployment_id,
                    RunnerTask.operation_id,
                )
            )
            exhausted_links = failed_result.all()
            exhausted_run_ids = tuple(
                row.run_id for row in exhausted_links if row.run_id is not None
            )
            exhausted_deployment_ids = tuple(
                row.deployment_id
                for row in exhausted_links
                if row.deployment_id is not None
            )
            exhausted_operation_ids = tuple(
                row.operation_id
                for row in exhausted_links
                if row.operation_id is not None
            )
            if retry_run_ids:
                await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id.in_(retry_run_ids),
                        PipelineRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                    )
                    .values(
                        status=RunStatus.QUEUED,
                        leased_by=None,
                        lease_expires_at=None,
                        heartbeat_at=now,
                        error_message=recovered_message,
                        version=PipelineRun.version + 1,
                    )
                )
            if retry_operation_ids:
                await uow.session.execute(
                    update(OperationRequest)
                    .where(
                        OperationRequest.id.in_(retry_operation_ids),
                        OperationRequest.state == ApprovalState.EXECUTING,
                    )
                    .values(state=ApprovalState.APPROVED)
                )
            retry_deployment_filters = []
            if retry_deployment_ids:
                retry_deployment_filters.append(
                    Deployment.id.in_(retry_deployment_ids)
                )
            if retry_run_ids:
                retry_deployment_filters.append(Deployment.run_id.in_(retry_run_ids))
            if retry_deployment_filters:
                await uow.session.execute(
                    update(Deployment)
                    .where(
                        or_(*retry_deployment_filters),
                        Deployment.status.in_(
                            (DeploymentStatus.PENDING, DeploymentStatus.DEPLOYING)
                        ),
                    )
                    .values(
                        status=DeploymentStatus.PENDING,
                        finished_at=None,
                        error_message=recovered_message,
                    )
                )
            if exhausted_run_ids:
                run_result = await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id.in_(exhausted_run_ids),
                        PipelineRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                    )
                    .values(
                        status=RunStatus.FAILED,
                        finished_at=now,
                        error_message=exhausted_message,
                        leased_by=None,
                        lease_expires_at=None,
                        version=PipelineRun.version + 1,
                    )
                    .returning(PipelineRun.id)
                )
                failed_run_ids = tuple(run_result.scalars().all())
            if exhausted_operation_ids:
                await uow.session.execute(
                    update(OperationRequest)
                    .where(
                        OperationRequest.id.in_(exhausted_operation_ids),
                        OperationRequest.state.in_(
                            (ApprovalState.APPROVED, ApprovalState.EXECUTING)
                        ),
                    )
                    .values(
                        state=ApprovalState.FAILED,
                        result={"error": exhausted_message},
                    )
                )
            deployment_filters = []
            if exhausted_deployment_ids:
                deployment_filters.append(Deployment.id.in_(exhausted_deployment_ids))
            if exhausted_run_ids:
                deployment_filters.append(Deployment.run_id.in_(exhausted_run_ids))
            if deployment_filters:
                deployment_result = await uow.session.execute(
                    update(Deployment)
                    .where(
                        or_(*deployment_filters),
                        Deployment.status.in_(
                            (DeploymentStatus.PENDING, DeploymentStatus.DEPLOYING)
                        ),
                    )
                    .values(
                        status=DeploymentStatus.FAILED,
                        finished_at=now,
                        error_message=exhausted_message,
                    )
                    .returning(Deployment.id)
                )
                failed_deployment_ids = tuple(deployment_result.scalars().all())
            recovered_count = len(retry_links) + len(exhausted_links)
        for run_id in sorted(set(failed_run_ids)):
            await self._notify_run_result(run_id, succeeded=False)
        for deployment_id in sorted(set(failed_deployment_ids)):
            await self._notify_deployment_result(deployment_id, succeeded=False)
        return recovered_count

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        kinds: Sequence[TaskKind] | None = None,
    ) -> TaskLease | None:
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.tasks is not None
            task = await uow.tasks.lease_next(
                worker_id, lease_seconds=lease_seconds, kinds=kinds
            )
            return _task_lease(task) if task is not None else None

    async def mark_running(self, lease: TaskLease) -> TaskLease | None:
        now = utcnow()
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            result = await uow.session.execute(
                update(RunnerTask)
                .where(
                    RunnerTask.id == lease.id,
                    RunnerTask.version == lease.version,
                    RunnerTask.leased_by == lease.leased_by,
                    RunnerTask.state == TaskState.LEASED,
                    RunnerTask.lease_expires_at > now,
                )
                .values(state=TaskState.RUNNING, version=RunnerTask.version + 1)
            )
            if affected_rows(result) != 1:
                return None
            if lease.run_id is not None:
                await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id == lease.run_id,
                        PipelineRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                    )
                    .values(
                        status=RunStatus.RUNNING,
                        leased_by=lease.leased_by,
                        lease_expires_at=lease.lease_expires_at,
                        heartbeat_at=now,
                        started_at=func.coalesce(PipelineRun.started_at, now),
                        version=PipelineRun.version + 1,
                    )
                )
            if lease.operation_id is not None:
                await uow.session.execute(
                    update(OperationRequest)
                    .where(
                        OperationRequest.id == lease.operation_id,
                        OperationRequest.state.in_(
                            (ApprovalState.APPROVED, ApprovalState.EXECUTING)
                        ),
                    )
                    .values(state=ApprovalState.EXECUTING)
                )
            task = await uow.session.get(RunnerTask, lease.id, populate_existing=True)
            return _task_lease(task) if task is not None else None

    async def heartbeat(self, lease: TaskLease, *, lease_seconds: int) -> TaskLease | None:
        from datetime import timedelta

        now = utcnow()
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            result = await uow.session.execute(
                update(RunnerTask)
                .where(
                    RunnerTask.id == lease.id,
                    RunnerTask.version == lease.version,
                    RunnerTask.leased_by == lease.leased_by,
                    RunnerTask.state.in_((TaskState.LEASED, TaskState.RUNNING)),
                    RunnerTask.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    version=RunnerTask.version + 1,
                )
            )
            if affected_rows(result) != 1:
                return None
            task = await uow.session.get(RunnerTask, lease.id, populate_existing=True)
            if lease.run_id is not None and task is not None:
                await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id == lease.run_id,
                        PipelineRun.leased_by == lease.leased_by,
                        PipelineRun.status == RunStatus.RUNNING,
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=task.lease_expires_at,
                        version=PipelineRun.version + 1,
                    )
                )
            return _task_lease(task) if task is not None else None

    async def cancellation_requested(self, lease: TaskLease) -> bool:
        if lease.run_id is None:
            return False
        async with self._session_factory() as session:
            value = await session.scalar(
                select(PipelineRun.cancel_requested).where(PipelineRun.id == lease.run_id)
            )
            return bool(value)

    async def finish(
        self,
        lease: TaskLease,
        *,
        state: TaskState,
        error_message: str | None = None,
    ) -> bool:
        ensure_task_transition(TaskState.RUNNING, state)
        if state not in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            raise ValueError("finish requires a terminal task state")
        now = utcnow()
        run_finished = False
        failed_deployment_ids: tuple[str, ...] = ()
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            result = await uow.session.execute(
                update(RunnerTask)
                .where(
                    RunnerTask.id == lease.id,
                    RunnerTask.version == lease.version,
                    RunnerTask.leased_by == lease.leased_by,
                    RunnerTask.state.in_((TaskState.LEASED, TaskState.RUNNING)),
                    RunnerTask.lease_expires_at > now,
                )
                .values(
                    state=state,
                    leased_by=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    error_message=error_message,
                    version=RunnerTask.version + 1,
                )
            )
            task_finished = affected_rows(result) == 1
            linked_deployment_id = (
                await uow.session.scalar(
                    select(RunnerTask.deployment_id).where(RunnerTask.id == lease.id)
                )
                if task_finished
                else None
            )
            if task_finished and lease.run_id is not None:
                run_state = {
                    TaskState.SUCCEEDED: RunStatus.SUCCEEDED,
                    TaskState.FAILED: RunStatus.FAILED,
                    TaskState.CANCELLED: RunStatus.CANCELLED,
                }[state]
                run_result = await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id == lease.run_id,
                        PipelineRun.leased_by == lease.leased_by,
                        PipelineRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                    )
                    .values(
                        status=run_state,
                        leased_by=None,
                        lease_expires_at=None,
                        heartbeat_at=now,
                        finished_at=now,
                        error_message=error_message,
                        version=PipelineRun.version + 1,
                    )
                )
                run_finished = affected_rows(run_result) == 1
            if task_finished and lease.operation_id is not None:
                operation_state = (
                    ApprovalState.SUCCEEDED
                    if state == TaskState.SUCCEEDED
                    else ApprovalState.FAILED
                )
                operation_result = {"task_id": lease.id}
                if error_message:
                    operation_result["error"] = error_message
                await uow.session.execute(
                    update(OperationRequest)
                    .where(
                        OperationRequest.id == lease.operation_id,
                        OperationRequest.state.in_(
                            (ApprovalState.APPROVED, ApprovalState.EXECUTING)
                        ),
                    )
                    .values(state=operation_state, result=operation_result)
                )
            if (
                task_finished
                and linked_deployment_id is not None
                and state != TaskState.SUCCEEDED
            ):
                deployment_result = await uow.session.execute(
                    update(Deployment)
                    .where(
                        Deployment.id == linked_deployment_id,
                        Deployment.status.in_(
                            (DeploymentStatus.PENDING, DeploymentStatus.DEPLOYING)
                        ),
                    )
                    .values(
                        status=DeploymentStatus.FAILED,
                        finished_at=now,
                        error_message=error_message or "runner task failed",
                    )
                    .returning(Deployment.id)
                )
                failed_deployment_ids = tuple(deployment_result.scalars().all())
        if run_finished and state in (TaskState.SUCCEEDED, TaskState.FAILED):
            await self._notify_run_result(
                lease.run_id,
                succeeded=state == TaskState.SUCCEEDED,
            )
        for deployment_id in failed_deployment_ids:
            await self._notify_deployment_result(deployment_id, succeeded=False)
        return task_finished

    async def _notify_run_result(self, run_id: str | None, *, succeeded: bool) -> None:
        if run_id is None or self._secret_manager is None:
            return
        await deliver_event(
            self._session_factory,
            self._secret_manager,
            run_result_notification(run_id=run_id, succeeded=succeeded),
            actor="system:runner",
        )

    async def _notify_deployment_result(
        self, deployment_id: str, *, succeeded: bool
    ) -> None:
        if self._secret_manager is None:
            return
        await deliver_event(
            self._session_factory,
            self._secret_manager,
            deployment_result_notification(
                deployment_id=deployment_id,
                succeeded=succeeded,
                status=(
                    DeploymentStatus.HEALTHY.value
                    if succeeded
                    else DeploymentStatus.FAILED.value
                ),
            ),
            actor="system:runner",
        )

    async def retry(
        self,
        lease: TaskLease,
        *,
        available_at: datetime,
        error_message: str,
    ) -> bool:
        now = utcnow()
        async with UnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            result = await uow.session.execute(
                update(RunnerTask)
                .where(
                    RunnerTask.id == lease.id,
                    RunnerTask.version == lease.version,
                    RunnerTask.leased_by == lease.leased_by,
                    RunnerTask.state.in_((TaskState.LEASED, TaskState.RUNNING)),
                    RunnerTask.attempts < RunnerTask.max_attempts,
                    RunnerTask.lease_expires_at > now,
                )
                .values(
                    state=TaskState.PENDING,
                    available_at=available_at,
                    leased_by=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    error_message=error_message,
                    version=RunnerTask.version + 1,
                )
            )
            if affected_rows(result) == 1 and lease.run_id is not None:
                await uow.session.execute(
                    update(PipelineRun)
                    .where(
                        PipelineRun.id == lease.run_id,
                        PipelineRun.leased_by == lease.leased_by,
                        PipelineRun.status == RunStatus.RUNNING,
                    )
                    .values(
                        status=RunStatus.QUEUED,
                        leased_by=None,
                        lease_expires_at=None,
                        heartbeat_at=now,
                        error_message=error_message,
                        version=PipelineRun.version + 1,
                    )
                )
            return affected_rows(result) == 1


class SQLAlchemyLogStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        log_dir: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._log_dir = log_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def append(self, event: LogEvent) -> None:
        if event.run_id is None:
            return
        lock = await self._lock_for(event.run_id)
        async with lock:
            last_error: IntegrityError | None = None
            for _ in range(3):
                async with self._session_factory() as session:
                    current = await session.scalar(
                        select(func.max(RunLog.sequence)).where(RunLog.run_id == event.run_id)
                    )
                    sequence = int(current or 0) + 1
                    created_at = event.created_at or utcnow()
                    session.add(
                        RunLog(
                            run_id=event.run_id,
                            sequence=sequence,
                            level=event.level,
                            stage=event.stage,
                            message=event.message,
                            created_at=created_at,
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError as exc:
                        await session.rollback()
                        last_error = exc
                        await asyncio.sleep(0)
                        continue
                    await self._append_jsonl(
                        run_id=event.run_id,
                        sequence=sequence,
                        level=event.level,
                        stage=event.stage,
                        message=event.message,
                        created_at=created_at,
                    )
                    return
            if last_error is not None:
                raise last_error

    async def _lock_for(self, run_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(run_id, asyncio.Lock())

    async def _append_jsonl(
        self,
        *,
        run_id: str,
        sequence: int,
        level: str,
        stage: str | None,
        message: str,
        created_at: datetime,
    ) -> None:
        if self._log_dir is None:
            return
        payload = {
            "sequence": sequence,
            "level": level,
            "stage": stage,
            "message": message,
            "created_at": created_at.isoformat(),
        }
        path = self._log_dir / "runs" / f"{run_id}.jsonl"
        try:
            await asyncio.to_thread(_append_json_line, path, payload)
        except Exception:
            logger.warning(
                "failed to append pipeline JSONL log for run %s",
                run_id,
                exc_info=True,
            )


def _task_lease(task: RunnerTask) -> TaskLease:
    if task.leased_by is None or task.lease_expires_at is None:
        raise ValueError("cannot map an unleased RunnerTask")
    return TaskLease(
        id=task.id,
        kind=task.kind,
        payload=dict(task.payload),
        resource_key=task.resource_key,
        attempts=task.attempts,
        max_attempts=task.max_attempts,
        leased_by=task.leased_by,
        lease_expires_at=task.lease_expires_at,
        run_id=task.run_id,
        deployment_id=task.deployment_id,
        operation_id=task.operation_id,
        version=task.version,
    )


def _append_json_line(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.write("\n")
