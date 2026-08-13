"""启动任务 Worker、指标 Worker、维护调度器和受保护的内部 API。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from dataclasses import dataclass

import uvicorn

from devops.config import get_settings
from devops.db import Database
from devops.domain.models import TaskKind
from devops.runner.engine import LeaseRunner
from devops.runner.handlers import (
    DeploymentTaskHandler,
    MetricsTaskHandler,
    PipelineTaskHandler,
    RunnerDependencies,
    ScriptTaskHandler,
)
from devops.runner.internal_api import create_internal_app
from devops.runner.process import AsyncCommandRunner
from devops.runner.scheduler import MaintenanceScheduler
from devops.runner.ssh import AsyncSSHConnector
from devops.runner.store import SQLAlchemyLogStore, SQLAlchemyRunnerTaskStore
from devops.security import SecretManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    worker_id: str
    poll_seconds: float
    heartbeat_seconds: float
    metrics_interval_seconds: float
    internal_token: str | None
    internal_host: str
    internal_port: int

    @classmethod
    def from_environment(cls, *, lease_seconds: int) -> RunnerSettings:
        worker_id = os.getenv("DEVOPS_RUNNER_ID") or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        poll = _positive_float("DEVOPS_RUNNER_POLL_SECONDS", 1.0)
        heartbeat_default = max(1.0, lease_seconds / 3)
        heartbeat = _positive_float("DEVOPS_RUNNER_HEARTBEAT_SECONDS", heartbeat_default)
        if heartbeat >= lease_seconds:
            raise ValueError("DEVOPS_RUNNER_HEARTBEAT_SECONDS must be shorter than the lease")
        metrics_interval = _positive_float("DEVOPS_METRICS_INTERVAL_SECONDS", 30.0)
        internal_token = os.getenv("DEVOPS_INTERNAL_TOKEN")
        internal_host = os.getenv("DEVOPS_INTERNAL_HOST", "127.0.0.1")
        internal_port = int(os.getenv("DEVOPS_INTERNAL_PORT", "8765"))
        if not 1 <= internal_port <= 65535:
            raise ValueError("DEVOPS_INTERNAL_PORT is out of range")
        return cls(
            worker_id=worker_id,
            poll_seconds=poll,
            heartbeat_seconds=heartbeat,
            metrics_interval_seconds=metrics_interval,
            internal_token=internal_token,
            internal_host=internal_host,
            internal_port=internal_port,
        )


async def runner_main() -> None:
    logging.basicConfig(
        level=os.getenv("DEVOPS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    runner_settings = RunnerSettings.from_environment(
        lease_seconds=settings.runner_lease_seconds
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    if settings.auto_create_schema:
        await database.create_schema()
    secrets = SecretManager.from_key_file(settings.secret_key_path)
    dependencies = RunnerDependencies(
        session_factory=database.session_factory,
        workspace_dir=settings.workspace_dir,
        secrets=secrets,
        commands=AsyncCommandRunner(),
        ssh=AsyncSSHConnector(),
    )
    handlers = {
        TaskKind.PIPELINE: PipelineTaskHandler(dependencies),
        TaskKind.DEPLOYMENT: DeploymentTaskHandler(dependencies),
        TaskKind.SCRIPT: ScriptTaskHandler(dependencies),
        TaskKind.METRICS: MetricsTaskHandler(dependencies),
    }
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    task_store = SQLAlchemyRunnerTaskStore(database.session_factory, secrets)
    log_store = SQLAlchemyLogStore(database.session_factory, settings.log_dir)
    runner = LeaseRunner(
        worker_id=runner_settings.worker_id,
        store=task_store,
        handlers=handlers,
        log_store=log_store,
        lease_seconds=settings.runner_lease_seconds,
        heartbeat_seconds=runner_settings.heartbeat_seconds,
        poll_seconds=runner_settings.poll_seconds,
    )
    metrics_runner = LeaseRunner(
        worker_id=f"{runner_settings.worker_id}:metrics",
        store=task_store,
        handlers={TaskKind.METRICS: handlers[TaskKind.METRICS]},
        log_store=log_store,
        lease_seconds=settings.runner_lease_seconds,
        heartbeat_seconds=runner_settings.heartbeat_seconds,
        poll_seconds=runner_settings.poll_seconds,
    )
    scheduler = MaintenanceScheduler(
        database.session_factory,
        interval_seconds=runner_settings.metrics_interval_seconds,
        log_retention_days=settings.run_log_retention_days,
        audit_retention_days=settings.audit_retention_days,
        log_dir=settings.log_dir,
    )
    logger.info("runner starting", extra={"worker_id": runner_settings.worker_id})
    scheduler_task = asyncio.create_task(scheduler.run(stop_event))
    internal_server: uvicorn.Server | None = None
    internal_task: asyncio.Task[None] | None = None
    if runner_settings.internal_token:
        internal_app = create_internal_app(
            dependencies=dependencies, token=runner_settings.internal_token
        )
        internal_server = uvicorn.Server(
            uvicorn.Config(
                internal_app,
                host=runner_settings.internal_host,
                port=runner_settings.internal_port,
                log_config=None,
                access_log=False,
            )
        )
        internal_task = asyncio.create_task(internal_server.serve())
        internal_task.add_done_callback(lambda _: stop_event.set())
    else:
        logger.warning("DEVOPS_INTERNAL_TOKEN is unset; Runner internal API is disabled")
    worker_tasks = (
        asyncio.create_task(
            runner.run_forever(
                stop_event=stop_event,
                kinds=(TaskKind.PIPELINE, TaskKind.DEPLOYMENT, TaskKind.SCRIPT),
            )
        ),
        asyncio.create_task(
            metrics_runner.run_forever(stop_event=stop_event, kinds=(TaskKind.METRICS,))
        ),
    )
    try:
        await asyncio.gather(*worker_tasks)
    finally:
        stop_event.set()
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        if internal_server is not None:
            internal_server.should_exit = True
        if internal_task is not None:
            internal_result = await asyncio.gather(internal_task, return_exceptions=True)
            if isinstance(internal_result[0], Exception):
                logger.error("Runner internal API stopped unexpectedly: %s", internal_result[0])
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await database.dispose()
        logger.info("runner stopped")


def run_runner() -> None:
    asyncio.run(runner_main())


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw is not None else default
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


if __name__ == "__main__":
    run_runner()
