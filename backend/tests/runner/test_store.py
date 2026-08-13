from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from devops.db import Database
from devops.domain.models import (
    ApprovalState,
    Deployment,
    DeploymentEnvironment,
    DeploymentStatus,
    OperationKind,
    OperationRequest,
    PipelineRun,
    Project,
    RunLog,
    RunnerTask,
    RunStatus,
    Server,
    TaskKind,
    TaskState,
)
from devops.runner.contracts import LogEvent, TaskLease
from devops.runner.scheduler import MaintenanceScheduler
from devops.runner.store import SQLAlchemyLogStore, SQLAlchemyRunnerTaskStore


async def test_store_rejects_stale_lease_completion(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runner.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            session.add(
                RunnerTask(
                    kind=TaskKind.SCRIPT,
                    resource_key="server:1:script",
                    payload={"server_id": "1"},
                )
            )
            await session.commit()

        store = SQLAlchemyRunnerTaskStore(database.session_factory)
        claimed = await store.claim_next(worker_id="worker-1", lease_seconds=30)
        assert claimed is not None
        running = await store.mark_running(claimed)
        assert running is not None
        renewed = await store.heartbeat(running, lease_seconds=30)
        assert renewed is not None

        assert not await store.finish(running, state=TaskState.SUCCEEDED)
        assert await store.finish(renewed, state=TaskState.SUCCEEDED)
    finally:
        await database.dispose()


async def test_store_recovers_expired_or_exhausted_tasks(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    try:
        async with database.session_factory() as session:
            retryable = RunnerTask(
                kind=TaskKind.METRICS,
                state=TaskState.RUNNING,
                resource_key="server:1:metrics",
                payload={"server_id": "1"},
                attempts=1,
                max_attempts=2,
                leased_by="dead",
                lease_expires_at=now - timedelta(seconds=1),
            )
            exhausted = RunnerTask(
                kind=TaskKind.METRICS,
                state=TaskState.LEASED,
                resource_key="server:2:metrics",
                payload={"server_id": "2"},
                attempts=2,
                max_attempts=2,
                leased_by="dead",
                lease_expires_at=now - timedelta(seconds=1),
            )
            session.add_all((retryable, exhausted))
            await session.commit()
            retryable_id = retryable.id
            exhausted_id = exhausted.id

        store = SQLAlchemyRunnerTaskStore(database.session_factory)
        assert await store.recover_expired(now) == 2
        async with database.session_factory() as session:
            recovered = await session.get(RunnerTask, retryable_id)
            failed = await session.get(RunnerTask, exhausted_id)
            assert recovered is not None and recovered.state == TaskState.PENDING
            assert recovered.leased_by is None
            assert failed is not None and failed.state == TaskState.FAILED
            assert failed.leased_by is None
    finally:
        await database.dispose()


async def test_exhausted_lease_closes_operation_and_deployment_state(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'closed-loop.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    try:
        async with database.session_factory() as session:
            project = Project(name="closed-loop", repo_url="https://example.test/repo.git")
            server = Server(name="node-1", host="node-1", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="production",
                deploy_path="/srv/closed-loop",
            )
            operation = OperationRequest(
                kind=OperationKind.DEPLOY,
                state=ApprovalState.EXECUTING,
                requested_by="admin:test",
                parameters={},
                parameter_hash="0" * 64,
                expires_at=now + timedelta(hours=1),
            )
            session.add_all((environment, operation))
            await session.flush()
            deployment = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.DEPLOYING,
                image_ref="registry.example.test/app:revision",
                image_digest="sha256:" + "a" * 64,
                revision="revision",
                environment_snapshot={},
            )
            session.add(deployment)
            await session.flush()
            task = RunnerTask(
                kind=TaskKind.DEPLOYMENT,
                state=TaskState.RUNNING,
                resource_key=f"project:{project.id}",
                payload={"action": "deploy"},
                deployment_id=deployment.id,
                operation_id=operation.id,
                attempts=1,
                max_attempts=1,
                leased_by="dead-runner",
                lease_expires_at=now - timedelta(seconds=1),
            )
            session.add(task)
            await session.commit()
            operation_id = operation.id
            deployment_id = deployment.id

        store = SQLAlchemyRunnerTaskStore(database.session_factory)
        assert await store.recover_expired(now) == 1

        async with database.session_factory() as session:
            operation = await session.get(OperationRequest, operation_id)
            deployment = await session.get(Deployment, deployment_id)
            assert operation is not None and operation.state == ApprovalState.FAILED
            assert "retry limit" in operation.result["error"]
            assert deployment is not None and deployment.status == DeploymentStatus.FAILED
            assert deployment.finished_at is not None
    finally:
        await database.dispose()


async def test_retryable_lease_rewinds_linked_states(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'retry-loop.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    try:
        async with database.session_factory() as session:
            project = Project(name="retry-loop", repo_url="https://example.test/retry.git")
            server = Server(name="node-retry", host="node-retry", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="production",
                deploy_path="/srv/retry-loop",
            )
            operation = OperationRequest(
                kind=OperationKind.DEPLOY,
                state=ApprovalState.EXECUTING,
                requested_by="admin:test",
                parameters={},
                parameter_hash="1" * 64,
                expires_at=now + timedelta(hours=1),
            )
            session.add_all((environment, operation))
            await session.flush()
            run = PipelineRun(
                project_id=project.id,
                environment_id=environment.id,
                status=RunStatus.RUNNING,
                trigger_type="test",
                commit_sha="a" * 40,
                ref="refs/heads/main",
                config_snapshot={},
                snapshot_sha256="2" * 64,
                leased_by="dead-runner",
                lease_expires_at=now - timedelta(seconds=1),
            )
            session.add(run)
            await session.flush()
            deployment = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                run_id=run.id,
                status=DeploymentStatus.DEPLOYING,
                image_ref="registry.example.test/app:revision",
                image_digest="sha256:" + "b" * 64,
                revision="revision",
                environment_snapshot={},
            )
            session.add(deployment)
            await session.flush()
            task = RunnerTask(
                kind=TaskKind.DEPLOYMENT,
                state=TaskState.RUNNING,
                resource_key=f"project:{project.id}",
                payload={"action": "deploy"},
                run_id=run.id,
                deployment_id=deployment.id,
                operation_id=operation.id,
                attempts=1,
                max_attempts=2,
                leased_by="dead-runner",
                lease_expires_at=now - timedelta(seconds=1),
            )
            session.add(task)
            await session.commit()
            identifiers = (task.id, run.id, deployment.id, operation.id)

        store = SQLAlchemyRunnerTaskStore(database.session_factory)
        assert await store.recover_expired(now) == 1

        async with database.session_factory() as session:
            task = await session.get(RunnerTask, identifiers[0])
            run = await session.get(PipelineRun, identifiers[1])
            deployment = await session.get(Deployment, identifiers[2])
            operation = await session.get(OperationRequest, identifiers[3])
            assert task is not None and task.state == TaskState.PENDING
            assert run is not None and run.status == RunStatus.QUEUED
            assert deployment is not None and deployment.status == DeploymentStatus.PENDING
            assert operation is not None and operation.state == ApprovalState.APPROVED
    finally:
        await database.dispose()


async def test_finish_uses_deployment_bound_after_the_lease_was_claimed(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'late-binding.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    try:
        async with database.session_factory() as session:
            project = Project(name="late-binding", repo_url="https://example.test/late.git")
            server = Server(name="late-node", host="late-node", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="production",
                deploy_path="/srv/late-binding",
            )
            session.add(environment)
            await session.flush()
            deployment = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.DEPLOYING,
                image_ref="registry.example.test/app:revision",
                image_digest="sha256:" + "c" * 64,
                revision="revision",
                environment_snapshot={},
            )
            task = RunnerTask(
                kind=TaskKind.PIPELINE,
                state=TaskState.RUNNING,
                resource_key=f"project:{project.id}",
                payload={},
                attempts=1,
                leased_by="runner-live",
                lease_expires_at=now + timedelta(minutes=1),
            )
            session.add_all((deployment, task))
            await session.commit()
            assert task.lease_expires_at is not None
            lease = TaskLease(
                id=task.id,
                kind=task.kind,
                payload=task.payload,
                resource_key=task.resource_key,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                leased_by="runner-live",
                lease_expires_at=task.lease_expires_at,
                version=task.version,
            )
            deployment_id = deployment.id
            task_id = task.id

        async with database.session_factory() as session:
            task = await session.get(RunnerTask, task_id)
            assert task is not None
            task.deployment_id = deployment_id
            await session.commit()

        store = SQLAlchemyRunnerTaskStore(database.session_factory)
        assert await store.finish(
            lease,
            state=TaskState.CANCELLED,
            error_message="cancelled during deployment",
        )

        async with database.session_factory() as session:
            deployment = await session.get(Deployment, deployment_id)
            assert deployment is not None
            assert deployment.status == DeploymentStatus.FAILED
            assert deployment.error_message == "cancelled during deployment"
    finally:
        await database.dispose()


async def test_log_store_appends_jsonl_after_database_commit(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'logs.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            project = Project(name="logs", repo_url="https://example.test/logs.git")
            session.add(project)
            await session.flush()
            run = PipelineRun(
                project_id=project.id,
                trigger_type="test",
                commit_sha="a" * 40,
                ref="refs/heads/main",
                config_snapshot={},
                snapshot_sha256="0" * 64,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        log_dir = tmp_path / "log-output"
        store = SQLAlchemyLogStore(database.session_factory, log_dir)
        await store.append(
            LogEvent(
                run_id=run_id,
                task_id="task-1",
                level="info",
                stage="build",
                message="already-redacted",
            )
        )

        record = json.loads((log_dir / "runs" / f"{run_id}.jsonl").read_text())
        assert record["sequence"] == 1
        assert record["message"] == "already-redacted"
        async with database.session_factory() as session:
            persisted = await session.scalar(select(RunLog).where(RunLog.run_id == run_id))
            assert persisted is not None

        blocked = tmp_path / "not-a-directory"
        blocked.write_text("occupied", encoding="utf-8")
        await SQLAlchemyLogStore(database.session_factory, blocked).append(
            LogEvent(
                run_id=run_id,
                task_id="task-1",
                level="warning",
                message="file failure must not roll back the database",
            )
        )
        async with database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(RunLog).where(RunLog.run_id == run_id)
                    )
                ).all()
            )
            assert len(rows) == 2
    finally:
        await database.dispose()


async def test_scheduler_removes_expired_jsonl_files(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cleanup.db').as_posix()}")
    await database.create_schema()
    try:
        log_dir = tmp_path / "logs"
        runs_dir = log_dir / "runs"
        runs_dir.mkdir(parents=True)
        expired = runs_dir / "expired.jsonl"
        expired.write_text("{}\n", encoding="utf-8")
        old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
        os.utime(expired, (old, old))

        scheduler = MaintenanceScheduler(
            database.session_factory,
            log_retention_days=1,
            log_dir=log_dir,
        )
        await scheduler.tick()

        assert not expired.exists()
    finally:
        await database.dispose()
