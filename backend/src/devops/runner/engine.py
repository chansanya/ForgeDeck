"""驱动持久任务租约、心跳、取消、重试和终态提交。

处理器只有在持续持有租约时才能执行副作用，丢失租约必须立即停止。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from devops.domain.models import TaskKind, TaskState
from devops.runner.contracts import (
    RunnerTaskStore,
    TaskExecutionContext,
    TaskHandler,
    TaskLease,
)
from devops.runner.logs import BoundedLogWriter


class RetryableTaskError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LeaseLostError(RuntimeError):
    pass


class TaskCancelledError(RuntimeError):
    pass


class _NullLogStore:
    async def append(self, event: object) -> None:
        return None


class LeaseRunner:
    def __init__(
        self,
        *,
        worker_id: str,
        store: RunnerTaskStore,
        handlers: Mapping[TaskKind, TaskHandler],
        log_store: object | None = None,
        lease_seconds: int = 60,
        heartbeat_seconds: float | None = None,
        poll_seconds: float = 1.0,
        recovery_seconds: float | None = None,
        cancel_grace_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        heartbeat_seconds = heartbeat_seconds or max(1.0, lease_seconds / 3)
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        if poll_seconds <= 0 or cancel_grace_seconds < 0:
            raise ValueError("invalid runner timing configuration")
        if recovery_seconds is None:
            recovery_seconds = max(poll_seconds, min(30.0, lease_seconds / 2))
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive")
        self._worker_id = worker_id
        self._store = store
        self._handlers = dict(handlers)
        self._log_store = log_store or _NullLogStore()
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._recovery_seconds = recovery_seconds
        self._cancel_grace_seconds = cancel_grace_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._next_recovery_at: float | None = None

    async def startup(self) -> int:
        """启动 Runner 前恢复过期租约，并安排下一次周期恢复。"""
        recovered = await self._store.recover_expired(self._now())
        self._next_recovery_at = (
            asyncio.get_running_loop().time() + self._recovery_seconds
        )
        return recovered

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        kinds: Sequence[TaskKind] | None = None,
    ) -> None:
        """持续领取任务；空闲时带抖动等待，避免多 Runner 同步轮询数据库。"""
        stop_event = stop_event or asyncio.Event()
        await self.startup()
        while not stop_event.is_set():
            await self._recover_if_due()
            claimed = await self.run_once(kinds=kinds)
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._jittered_poll_interval())
            except TimeoutError:
                pass

    async def _recover_if_due(self) -> int:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_recovery_at is not None and now < self._next_recovery_at:
            return 0
        recovered = await self._store.recover_expired(self._now())
        self._next_recovery_at = now + self._recovery_seconds
        return recovered

    async def run_once(self, *, kinds: Sequence[TaskKind] | None = None) -> bool:
        """尝试领取并执行一个任务，返回是否成功领取到任务。"""
        lease = await self._store.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            kinds=kinds,
        )
        if lease is None:
            return False

        running = await self._store.mark_running(lease)
        if running is None:
            return True
        handler = self._handlers.get(running.kind)
        if handler is None:
            await self._store.finish(
                running,
                state=TaskState.FAILED,
                error_message=f"no runner handler registered for task kind {running.kind.value}",
            )
            return True

        await self._execute(running, handler)
        return True

    async def _execute(self, lease: TaskLease, handler: TaskHandler) -> None:
        cancel_event = asyncio.Event()
        writer = BoundedLogWriter(
            self._log_store,  # type: ignore[arg-type]
            task_id=lease.id,
            run_id=lease.run_id,
        )
        context = TaskExecutionContext(lease=lease, cancel_event=cancel_event, log=writer)
        # handler 与租约监控并行运行。监控先结束代表取消或所有权丢失，
        # 此时必须先通知 handler 停止，不能继续产生 Docker/SSH 副作用。
        lease_box = [lease]
        handler_task = asyncio.create_task(handler(context))
        monitor_task = asyncio.create_task(self._monitor_lease(lease_box, cancel_event))

        try:
            done, _ = await asyncio.wait(
                {handler_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor_task in done:
                reason = await monitor_task
                cancel_event.set()
                await self._stop_handler(handler_task)
                if reason == "cancelled":
                    await self._store.finish(lease_box[0], state=TaskState.CANCELLED)
                return

            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            try:
                await handler_task
            except TaskCancelledError:
                await self._store.finish(lease_box[0], state=TaskState.CANCELLED)
            except RetryableTaskError as exc:
                if lease_box[0].attempts < lease_box[0].max_attempts:
                    available_at = self._now() + timedelta(seconds=exc.retry_after_seconds)
                    await self._store.retry(
                        lease_box[0], available_at=available_at, error_message=str(exc)
                    )
                else:
                    await self._store.finish(
                        lease_box[0], state=TaskState.FAILED, error_message=str(exc)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._store.finish(
                    lease_box[0], state=TaskState.FAILED, error_message=self._safe_error(exc)
                )
            else:
                await self._store.finish(lease_box[0], state=TaskState.SUCCEEDED)
        except asyncio.CancelledError:
            cancel_event.set()
            monitor_task.cancel()
            await self._stop_handler(handler_task)
            raise
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

    async def _monitor_lease(
        self, lease_box: list[TaskLease], cancel_event: asyncio.Event
    ) -> str:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            lease = lease_box[0]
            if await self._store.cancellation_requested(lease):
                return "cancelled"
            renewed = await self._store.heartbeat(lease, lease_seconds=self._lease_seconds)
            # CAS 心跳失败说明任务已被恢复或转交，当前进程不再有权提交终态。
            if renewed is None:
                return "lease_lost"
            lease_box[0] = renewed
            cancel_event.clear()

    async def _stop_handler(self, task: asyncio.Task[None]) -> None:
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._cancel_grace_seconds)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:
            return

    def _jittered_poll_interval(self) -> float:
        return self._poll_seconds * random.uniform(0.9, 1.1)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = f"{type(exc).__name__}: {exc}"
        return message[:4000]
