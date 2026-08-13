"""定义 Runner 任务租约、处理器、日志存储和命令执行协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from devops.domain.models import TaskKind, TaskState


@dataclass(frozen=True, slots=True)
class TaskLease:
    id: str
    kind: TaskKind
    payload: Mapping[str, Any]
    resource_key: str
    attempts: int
    max_attempts: int
    leased_by: str
    lease_expires_at: datetime
    run_id: str | None = None
    deployment_id: str | None = None
    operation_id: str | None = None
    version: int = 0


class RunnerTaskStore(Protocol):
    """Persistence boundary used by the runner.

    Implementations must use compare-and-swap semantics for ownership-sensitive
    operations. Returning ``None``/``False`` means the caller no longer owns the
    lease and must stop all side effects.
    """

    async def recover_expired(self, now: datetime) -> int: ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        kinds: Sequence[TaskKind] | None = None,
    ) -> TaskLease | None: ...

    async def mark_running(self, lease: TaskLease) -> TaskLease | None: ...

    async def heartbeat(self, lease: TaskLease, *, lease_seconds: int) -> TaskLease | None: ...

    async def cancellation_requested(self, lease: TaskLease) -> bool: ...

    async def finish(
        self,
        lease: TaskLease,
        *,
        state: TaskState,
        error_message: str | None = None,
    ) -> bool: ...

    async def retry(
        self,
        lease: TaskLease,
        *,
        available_at: datetime,
        error_message: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LogEvent:
    run_id: str | None
    task_id: str
    level: str
    message: str
    stage: str | None = None
    created_at: datetime | None = None


class LogEventStore(Protocol):
    async def append(self, event: LogEvent) -> None: ...


@dataclass(slots=True)
class TaskExecutionContext:
    lease: TaskLease
    cancel_event: Any
    log: Any
    values: dict[str, Any] = field(default_factory=dict)


class TaskHandler(Protocol):
    async def __call__(self, context: TaskExecutionContext) -> None: ...
