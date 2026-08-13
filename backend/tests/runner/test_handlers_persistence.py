from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from devops.db import Database
from devops.domain.models import (
    Deployment,
    DeploymentEnvironment,
    DeploymentStatus,
    PipelineRun,
    Project,
    RunnerTask,
    Server,
    TaskKind,
    TaskState,
)
from devops.runner.contracts import TaskExecutionContext, TaskLease
from devops.runner.deploy import DeploymentError
from devops.runner.handlers import (
    DeploymentTaskHandler,
    PipelineTaskHandler,
    RunnerDependencies,
    _encode_env,
)
from devops.runner.process import AsyncCommandRunner
from devops.runner.source import canonical_snapshot
from devops.runner.ssh import AsyncSSHConnector


async def test_pipeline_deployment_persists_immutable_compose_snapshot(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'snapshot.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            project = Project(name="demo", repo_url="https://example.test/demo.git")
            server = Server(name="node", host="node", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="prod",
                deploy_path="/srv/demo",
            )
            run = PipelineRun(
                project_id=project.id,
                environment_id=environment.id,
                trigger_type="test",
                commit_sha="a" * 40,
                ref="refs/heads/main",
                config_snapshot={},
                snapshot_sha256="0" * 64,
            )
            session.add_all((environment, run))
            await session.flush()
            lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            task = RunnerTask(
                kind=TaskKind.PIPELINE,
                state=TaskState.RUNNING,
                resource_key=f"project:{project.id}",
                payload={},
                run_id=run.id,
                attempts=1,
                leased_by="runner-test",
                lease_expires_at=lease_expires_at,
            )
            session.add(task)
            await session.commit()
            identifiers = (
                run.id,
                project.id,
                environment.id,
                server.id,
                task.id,
                lease_expires_at,
            )

        dependencies = RunnerDependencies(
            session_factory=database.session_factory,
            workspace_dir=tmp_path / "workspaces",
            secrets=Mock(),
            commands=AsyncCommandRunner(),
            ssh=AsyncSSHConnector(),
        )
        handler = PipelineTaskHandler(dependencies)
        compose = b"services: {app: {image: ignored}}\n"
        snapshot = {
            "id": identifiers[2],
            "project_id": identifiers[1],
            "server_id": identifiers[3],
            "deploy_path": "/srv/demo",
            "env_config": {"MODE": "prod"},
            "healthcheck": {"kind": "compose"},
        }
        context = TaskExecutionContext(
            lease=TaskLease(
                id=identifiers[4],
                kind=TaskKind.PIPELINE,
                payload={},
                resource_key=f"project:{identifiers[1]}",
                attempts=1,
                max_attempts=3,
                leased_by="runner-test",
                lease_expires_at=identifiers[5],
                run_id=identifiers[0],
            ),
            cancel_event=Mock(),
            log=Mock(),
        )
        deployment_id, deployment_status = await handler._create_pipeline_deployment(
            context,
            run_id=identifiers[0],
            project_id=identifiers[1],
            environment_id=identifiers[2],
            server_id=identifiers[3],
            image_ref="registry.example.test/demo:abc",
            image_digest="sha256:" + "b" * 64,
            revision="a" * 40,
            compose_content=compose,
            environment_snapshot=snapshot,
        )
        recovered_id, recovered_status = await handler._create_pipeline_deployment(
            context,
            run_id=identifiers[0],
            project_id=identifiers[1],
            environment_id=identifiers[2],
            server_id=identifiers[3],
            image_ref="registry.example.test/demo:abc",
            image_digest="sha256:" + "b" * 64,
            revision="a" * 40,
            compose_content=compose,
            environment_snapshot=snapshot,
        )

        async with database.session_factory() as session:
            deployment = await session.get(Deployment, deployment_id)
            assert deployment is not None
            assert deployment.compose_content == compose.decode()
            assert deployment.compose_sha256 == hashlib.sha256(compose).hexdigest()
            assert deployment.environment_snapshot == snapshot
            assert recovered_id == deployment_id
            assert deployment_status == DeploymentStatus.DEPLOYING
            assert recovered_status == DeploymentStatus.DEPLOYING
            task = await session.get(RunnerTask, identifiers[4])
            assert task is not None and task.deployment_id == deployment_id
            count = await session.scalar(
                select(func.count()).select_from(Deployment).where(Deployment.run_id == identifiers[0])
            )
            assert count == 1
    finally:
        await database.dispose()


async def test_recovered_pipeline_reuses_checkpointed_image_digest(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'artifact-checkpoint.db').as_posix()}")
    await database.create_schema()
    commit_sha = "a" * 40
    image_ref = f"registry.example.test/checkpoint:{commit_sha[:12]}"
    image_digest = "sha256:" + "d" * 64
    try:
        async with database.session_factory() as session:
            project = Project(
                name="checkpoint",
                repo_url="https://example.test/checkpoint.git",
                image_repository="registry.example.test/checkpoint",
            )
            session.add(project)
            await session.flush()
            snapshot = {
                "project": {
                    "id": project.id,
                    "repo_url": project.repo_url,
                    "image_repository": project.image_repository,
                    "git_credential_id": None,
                    "registry_credential_id": None,
                    "pipeline_config": {},
                }
            }
            _, snapshot_hash = canonical_snapshot(snapshot)
            run = PipelineRun(
                project_id=project.id,
                trigger_type="test",
                commit_sha=commit_sha,
                ref="refs/heads/main",
                config_snapshot=snapshot,
                snapshot_sha256=snapshot_hash,
                image_ref=image_ref,
                image_digest=image_digest,
                current_stage="registry",
            )
            session.add(run)
            await session.commit()
            identifiers = (project.id, run.id, snapshot_hash)

        dependencies = RunnerDependencies(
            session_factory=database.session_factory,
            workspace_dir=tmp_path / "workspaces",
            secrets=Mock(),
            commands=AsyncCommandRunner(),
            ssh=AsyncSSHConnector(),
        )
        handler = PipelineTaskHandler(dependencies)
        source_directory = tmp_path / "checked-out-source"
        source_directory.mkdir()
        handler._source.checkout = AsyncMock(
            return_value=SimpleNamespace(directory=source_directory)
        )
        handler._builder.build_and_push = AsyncMock(
            side_effect=AssertionError("checkpointed image must not be rebuilt")
        )
        log = Mock()
        log.write = AsyncMock()
        log.add_secrets = Mock()
        context = TaskExecutionContext(
            lease=TaskLease(
                id="task-checkpoint",
                kind=TaskKind.PIPELINE,
                payload={
                    "run_id": identifiers[1],
                    "project_id": identifiers[0],
                    "environment_id": None,
                    "commit_sha": commit_sha,
                    "ref": "refs/heads/main",
                    "config_snapshot": snapshot,
                    "snapshot_sha256": identifiers[2],
                },
                resource_key=f"project:{identifiers[0]}",
                attempts=2,
                max_attempts=3,
                leased_by="runner-test",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                run_id=identifiers[1],
            ),
            cancel_event=asyncio.Event(),
            log=log,
        )

        await handler(context)

        handler._builder.build_and_push.assert_not_awaited()
        async with database.session_factory() as session:
            run = await session.get(PipelineRun, identifiers[1])
            assert run is not None
            assert run.image_ref == image_ref
            assert run.image_digest == image_digest
            assert run.current_stage == "complete"
    finally:
        await database.dispose()


async def test_explicit_rollback_failure_restored_by_deployer_keeps_deployment_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rollback-state.db').as_posix()}")
    await database.create_schema()
    try:
        compose = "services:\n  app:\n    image: placeholder\n"
        compose_sha256 = hashlib.sha256(compose.encode()).hexdigest()
        now = datetime.now(UTC)
        async with database.session_factory() as session:
            project = Project(name="rollback-demo", repo_url="https://example.test/demo.git")
            server = Server(name="rollback-node", host="node", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="prod",
                deploy_path="/srv/demo",
            )
            session.add(environment)
            await session.flush()
            snapshot = {
                "id": environment.id,
                "project_id": project.id,
                "server_id": server.id,
                "name": "prod",
                "deploy_path": "/srv/demo",
                "env_config": {},
                "healthcheck": {"kind": "compose"},
            }
            target = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.HEALTHY,
                image_ref="registry.example.test/demo:previous",
                image_digest="sha256:" + "a" * 64,
                revision="revision-1",
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                created_at=now - timedelta(minutes=1),
            )
            session.add(target)
            await session.flush()
            current = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.HEALTHY,
                image_ref="registry.example.test/demo:current",
                image_digest="sha256:" + "b" * 64,
                revision="revision-2",
                previous_revision=target.revision,
                previous_deployment_id=target.id,
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                created_at=now,
            )
            session.add(current)
            await session.commit()
            identifiers = (project.id, server.id, target.id, current.id)

        async def load_target(*_: object, **__: object) -> tuple[object, object, tuple[str, ...]]:
            return object(), object(), ()

        async def fail_after_restoration(*_: object, **__: object) -> None:
            raise DeploymentError("rollback target unhealthy", rolled_back=True)

        @asynccontextmanager
        async def connected_session() -> AsyncIterator[Mock]:
            yield Mock()

        monkeypatch.setattr("devops.runner.handlers._load_ssh_target", load_target)
        monkeypatch.setattr(
            "devops.runner.handlers.ComposeDeployer.deploy", fail_after_restoration
        )
        ssh = Mock()
        ssh.connect.return_value = connected_session()
        handler = DeploymentTaskHandler(
            RunnerDependencies(
                session_factory=database.session_factory,
                workspace_dir=tmp_path / "workspaces",
                secrets=Mock(),
                commands=AsyncCommandRunner(),
                ssh=ssh,
            )
        )
        payload = {
            "deployment_id": identifiers[3],
            "target_deployment_id": identifiers[2],
            "project_id": identifiers[0],
            "target_revision": "revision-1",
            "target_image_ref": "registry.example.test/demo:previous",
            "target_image_digest": "sha256:" + "a" * 64,
            "compose_content": compose,
            "compose_sha256": compose_sha256,
            "environment_snapshot": snapshot,
            "service_name": "app",
        }

        with pytest.raises(DeploymentError, match="rollback target unhealthy") as caught:
            await handler._rollback(Mock(log=Mock()), payload)

        assert caught.value.rolled_back
        async with database.session_factory() as session:
            target = await session.get(Deployment, identifiers[2])
            current = await session.get(Deployment, identifiers[3])
            assert target is not None and target.status == DeploymentStatus.HEALTHY
            assert current is not None
            assert current.status == DeploymentStatus.HEALTHY
            assert current.error_message == "rollback target unhealthy"
            assert current.finished_at is not None
    finally:
        await database.dispose()


async def test_standalone_deploy_resolves_previous_revision_at_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'execution-order.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    compose = "services:\n  app:\n    image: placeholder\n"
    compose_sha256 = hashlib.sha256(compose.encode()).hexdigest()
    try:
        async with database.session_factory() as session:
            project = Project(name="ordered", repo_url="https://example.test/ordered.git")
            server = Server(name="ordered-node", host="node", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="production",
                deploy_path="/srv/ordered",
            )
            session.add(environment)
            await session.flush()
            snapshot = {
                "id": environment.id,
                "project_id": project.id,
                "server_id": server.id,
                "deploy_path": environment.deploy_path,
                "env_config": {},
                "healthcheck": {"kind": "compose"},
            }
            first = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.HEALTHY,
                image_ref="registry.example.test/app:first",
                image_digest="sha256:" + "1" * 64,
                revision="first",
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                created_at=now - timedelta(minutes=3),
                finished_at=now - timedelta(minutes=3),
            )
            session.add(first)
            await session.flush()
            second = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.PENDING,
                image_ref="registry.example.test/app:second",
                image_digest="sha256:" + "2" * 64,
                revision="second",
                previous_revision=first.revision,
                previous_deployment_id=first.id,
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                created_at=now - timedelta(minutes=2),
            )
            third = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.PENDING,
                image_ref="registry.example.test/app:third",
                image_digest="sha256:" + "3" * 64,
                revision="third",
                previous_revision=first.revision,
                previous_deployment_id=first.id,
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                created_at=now - timedelta(minutes=1),
            )
            session.add_all((second, third))
            await session.commit()
            identifiers = (project.id, first.id, second.id, third.id)

        handler = DeploymentTaskHandler(
            RunnerDependencies(
                session_factory=database.session_factory,
                workspace_dir=tmp_path / "workspaces",
                secrets=Mock(),
                commands=AsyncCommandRunner(),
                ssh=AsyncSSHConnector(),
            )
        )
        execute = AsyncMock()
        monkeypatch.setattr(handler, "_execute_deployment", execute)

        def payload(deployment_id: str, image_ref: str, digest: str, revision: str):
            return {
                "deployment_id": deployment_id,
                "project_id": identifiers[0],
                "image_ref": image_ref,
                "image_digest": digest,
                "revision": revision,
                "compose_content": compose,
                "compose_sha256": compose_sha256,
                "environment_snapshot": snapshot,
                "service_name": "app",
            }

        await handler._deploy(
            Mock(log=Mock()),
            payload(
                identifiers[2],
                "registry.example.test/app:second",
                "sha256:" + "2" * 64,
                "second",
            ),
        )
        async with database.session_factory() as session:
            second = await session.get(Deployment, identifiers[2])
            assert second is not None
            assert second.previous_deployment_id == identifiers[1]
            second.status = DeploymentStatus.HEALTHY
            second.finished_at = now
            await session.commit()

        await handler._deploy(
            Mock(log=Mock()),
            payload(
                identifiers[3],
                "registry.example.test/app:third",
                "sha256:" + "3" * 64,
                "third",
            ),
        )
        async with database.session_factory() as session:
            third = await session.get(Deployment, identifiers[3])
            assert third is not None
            assert third.previous_deployment_id == identifiers[2]
            assert third.previous_revision == "second"
    finally:
        await database.dispose()


async def test_completed_explicit_rollback_is_idempotent_after_runner_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rollback-idempotent.db').as_posix()}")
    await database.create_schema()
    compose = "services:\n  app:\n    image: placeholder\n"
    compose_sha256 = hashlib.sha256(compose.encode()).hexdigest()
    try:
        async with database.session_factory() as session:
            project = Project(name="rollback-idempotent", repo_url="https://example.test/r.git")
            server = Server(name="rollback-node", host="node", username="root")
            session.add_all((project, server))
            await session.flush()
            environment = DeploymentEnvironment(
                project_id=project.id,
                server_id=server.id,
                name="production",
                deploy_path="/srv/rollback-idempotent",
            )
            session.add(environment)
            await session.flush()
            snapshot = {
                "id": environment.id,
                "project_id": project.id,
                "server_id": server.id,
                "deploy_path": environment.deploy_path,
                "env_config": {},
                "healthcheck": {"kind": "compose"},
            }
            target = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.HEALTHY,
                image_ref="registry.example.test/app:target",
                image_digest="sha256:" + "4" * 64,
                revision="target",
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                finished_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.add(target)
            await session.flush()
            source = Deployment(
                project_id=project.id,
                environment_id=environment.id,
                server_id=server.id,
                status=DeploymentStatus.ROLLED_BACK,
                image_ref="registry.example.test/app:source",
                image_digest="sha256:" + "5" * 64,
                revision="source",
                previous_revision=target.revision,
                previous_deployment_id=target.id,
                compose_content=compose,
                compose_sha256=compose_sha256,
                environment_snapshot=snapshot,
                finished_at=datetime.now(UTC),
            )
            session.add(source)
            await session.commit()
            identifiers = (project.id, target.id, source.id)

        handler = DeploymentTaskHandler(
            RunnerDependencies(
                session_factory=database.session_factory,
                workspace_dir=tmp_path / "workspaces",
                secrets=Mock(),
                commands=AsyncCommandRunner(),
                ssh=AsyncSSHConnector(),
            )
        )
        execute = AsyncMock()
        monkeypatch.setattr(handler, "_execute_deployment", execute)

        await handler._rollback(
            Mock(log=Mock()),
            {
                "deployment_id": identifiers[2],
                "target_deployment_id": identifiers[1],
                "project_id": identifiers[0],
                "target_revision": "target",
                "target_image_ref": "registry.example.test/app:target",
                "target_image_digest": "sha256:" + "4" * 64,
                "compose_content": compose,
                "compose_sha256": compose_sha256,
                "environment_snapshot": snapshot,
                "service_name": "app",
            },
        )

        execute.assert_not_awaited()
    finally:
        await database.dispose()


def test_compose_env_escapes_literal_dollar_signs() -> None:
    assert _encode_env({"VALUE": "pa$$word-${TOKEN}"}) == b'VALUE="pa$$$$word-$${TOKEN}"\n'
