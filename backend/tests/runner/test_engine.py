from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from devops.domain.models import TaskKind, TaskState
from devops.runner.contracts import LogEvent, TaskExecutionContext, TaskLease
from devops.runner.engine import LeaseRunner, RetryableTaskError, TaskCancelledError


class FakeStore:
    def __init__(self, lease: TaskLease) -> None:
        self.lease = lease
        self.recoveries = 0
        self.claimed = False
        self.finished: list[tuple[TaskState, str | None]] = []
        self.retries: list[str] = []
        self.cancel_requested = False
        self.lose_lease = False

    async def recover_expired(self, now: datetime) -> int:
        self.recoveries += 1
        return 0

    async def claim_next(self, **_: object) -> TaskLease | None:
        if self.claimed:
            return None
        self.claimed = True
        return self.lease

    async def mark_running(self, lease: TaskLease) -> TaskLease | None:
        self.lease = replace(lease, version=lease.version + 1)
        return self.lease

    async def heartbeat(self, lease: TaskLease, **_: object) -> TaskLease | None:
        if self.lose_lease:
            return None
        self.lease = replace(
            lease,
            version=lease.version + 1,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        return self.lease

    async def cancellation_requested(self, lease: TaskLease) -> bool:
        return self.cancel_requested

    async def finish(
        self, lease: TaskLease, *, state: TaskState, error_message: str | None = None
    ) -> bool:
        self.finished.append((state, error_message))
        return True

    async def retry(
        self, lease: TaskLease, *, available_at: datetime, error_message: str
    ) -> bool:
        self.retries.append(error_message)
        return True


class MemoryLogs:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    async def append(self, event: LogEvent) -> None:
        self.events.append(event)


def lease() -> TaskLease:
    return TaskLease(
        id="task-1",
        kind=TaskKind.PIPELINE,
        payload={"run_id": "run-1"},
        resource_key="project:1",
        attempts=1,
        max_attempts=3,
        leased_by="worker-1",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
        run_id="run-1",
        version=1,
    )


async def test_runner_marks_success_after_handler_finishes() -> None:
    store = FakeStore(lease())

    async def handler(context: TaskExecutionContext) -> None:
        await context.log.write("done")

    runner = LeaseRunner(
        worker_id="worker-1",
        store=store,
        handlers={TaskKind.PIPELINE: handler},
        log_store=MemoryLogs(),
        lease_seconds=1,
        heartbeat_seconds=0.05,
    )
    assert await runner.run_once()
    assert store.finished == [(TaskState.SUCCEEDED, None)]


async def test_runner_retries_retryable_failure() -> None:
    store = FakeStore(lease())

    async def handler(context: TaskExecutionContext) -> None:
        raise RetryableTaskError("registry unavailable", retry_after_seconds=0)

    runner = LeaseRunner(
        worker_id="worker-1",
        store=store,
        handlers={TaskKind.PIPELINE: handler},
        lease_seconds=1,
        heartbeat_seconds=0.05,
    )
    await runner.run_once()
    assert store.retries == ["registry unavailable"]
    assert not store.finished


async def test_runner_does_not_write_terminal_state_after_lease_loss() -> None:
    store = FakeStore(lease())
    store.lose_lease = True

    async def handler(context: TaskExecutionContext) -> None:
        await context.cancel_event.wait()
        raise TaskCancelledError("cancelled")

    runner = LeaseRunner(
        worker_id="worker-1",
        store=store,
        handlers={TaskKind.PIPELINE: handler},
        lease_seconds=1,
        heartbeat_seconds=0.01,
        cancel_grace_seconds=0.1,
    )
    await runner.run_once()
    assert not store.finished


async def test_runner_persists_requested_cancellation() -> None:
    store = FakeStore(lease())
    store.cancel_requested = True

    async def handler(context: TaskExecutionContext) -> None:
        await context.cancel_event.wait()

    runner = LeaseRunner(
        worker_id="worker-1",
        store=store,
        handlers={TaskKind.PIPELINE: handler},
        lease_seconds=1,
        heartbeat_seconds=0.01,
        cancel_grace_seconds=0.1,
    )
    await runner.run_once()
    assert store.finished == [(TaskState.CANCELLED, None)]


async def test_runner_recovers_expired_leases_periodically() -> None:
    store = FakeStore(lease())
    store.claimed = True
    stop_event = asyncio.Event()
    runner = LeaseRunner(
        worker_id="worker-1",
        store=store,
        handlers={},
        lease_seconds=1,
        heartbeat_seconds=0.05,
        poll_seconds=0.005,
        recovery_seconds=0.01,
    )

    task = asyncio.create_task(runner.run_forever(stop_event=stop_event))
    await asyncio.sleep(0.04)
    stop_event.set()
    await task

    assert store.recoveries >= 2
