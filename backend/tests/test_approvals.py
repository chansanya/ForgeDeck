from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from devops.domain.models import (
    Deployment,
    DeploymentStatus,
    OperationKind,
    PipelineRun,
    Project,
    RunnerTask,
    TaskKind,
)
from devops.services import create_operation_request, project_snapshot, sha256_json


async def _create_ready_server(
    client: AsyncClient,
    auth_headers: dict[str, str],
    *,
    name: str,
    host: str,
):
    credential = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={"name": f"{name}-ssh", "kind": "ssh", "secret": "ssh-password"},
    )
    assert credential.status_code == 201, credential.text
    return await client.post(
        "/api/v1/servers",
        headers=auth_headers,
        json={
            "name": name,
            "host": host,
            "username": "deploy",
            "ssh_credential_id": credential.json()["id"],
            "host_key": "SHA256:test-host-key",
        },
    )


async def test_script_execution_requires_hash_bound_approval(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    server = await _create_ready_server(
        client,
        auth_headers,
        name="staging-1",
        host="10.0.0.10",
    )
    assert server.status_code == 201, server.text
    script = await client.post(
        "/api/v1/scripts",
        headers=auth_headers,
        json={"name": "restart-service", "content": "docker compose restart app"},
    )
    assert script.status_code == 201, script.text

    requested = await client.post(
        f"/api/v1/scripts/{script.json()['id']}/executions",
        headers=auth_headers,
        json={"server_id": server.json()["id"], "arguments": {"service": "app"}},
    )
    assert requested.status_code == 202, requested.text
    approval = requested.json()

    wrong = await client.post(
        f"/api/v1/approvals/{approval['id']}/approve",
        headers=auth_headers,
        json={"parameter_hash": "0" * 64},
    )
    assert wrong.status_code == 409

    accepted = await client.post(
        f"/api/v1/approvals/{approval['id']}/approve",
        headers=auth_headers,
        json={"parameter_hash": approval["parameter_hash"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "approved"

    async with app.state.database.session_factory() as session:
        task = await session.scalar(
            select(RunnerTask).where(RunnerTask.operation_id == approval["id"])
        )
        assert task is not None
        assert task.kind == TaskKind.SCRIPT
        assert task.max_attempts == 1
        assert task.payload["script_sha256"] == script.json()["sha256"]


async def test_repository_compose_must_be_frozen_before_deployment_approval(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    server = await _create_ready_server(
        client,
        auth_headers,
        name="prod-1",
        host="10.0.0.20",
    )
    project = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "billing",
            "repo_url": "https://example.com/billing.git",
            "image_repository": "registry.example.com/billing",
            "pipeline_config": {"service_name": "billing-api"},
        },
    )
    environment = await client.post(
        f"/api/v1/projects/{project.json()['id']}/environments",
        headers=auth_headers,
        json={
            "name": "production",
            "server_id": server.json()["id"],
            "compose_source": "repository",
            "compose_path": "deploy/compose.yaml",
            "deploy_path": "/srv/billing",
        },
    )
    request_body = {
        "environment_id": environment.json()["id"],
        "image_ref": "registry.example.com/billing:42",
        "image_digest": "sha256:" + "d" * 64,
        "revision": "42",
    }
    mutable = await client.post(
        "/api/v1/deployments/requests", headers=auth_headers, json=request_body
    )
    assert mutable.status_code == 422

    compose = "services:\n  app:\n    image: placeholder\n"
    frozen = await client.post(
        "/api/v1/deployments/requests",
        headers=auth_headers,
        json={**request_body, "compose_content": compose},
    )
    assert frozen.status_code == 202, frozen.text
    approval = frozen.json()
    accepted = await client.post(
        f"/api/v1/approvals/{approval['id']}/approve",
        headers=auth_headers,
        json={"parameter_hash": approval["parameter_hash"]},
    )
    assert accepted.status_code == 200, accepted.text

    async with app.state.database.session_factory() as session:
        task = await session.scalar(
            select(RunnerTask).where(RunnerTask.operation_id == approval["id"])
        )
        deployment = await session.scalar(
            select(Deployment).where(Deployment.id == task.deployment_id)
        )
        assert task.payload["compose_content"] == compose
        assert task.payload["compose_sha256"] == deployment.compose_sha256
        assert task.payload["service_name"] == "billing-api"
        assert task.payload["registry_credential_id"] is None
        assert task.resource_key == f"project:{project.json()['id']}"
        assert deployment.environment_snapshot["deploy_path"] == "/srv/billing"


async def test_mcp_build_approval_creates_pipeline_task(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "mcp-build",
            "repo_url": "https://example.com/mcp-build.git",
            "dockerfile_source": "inline",
            "dockerfile_content": "FROM scratch",
            "image_repository": "registry.example.com/mcp-build",
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    async with app.state.database.session_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        snapshot = project_snapshot(project, None)
        parameters = {
            "project_id": project.id,
            "commit_sha": "a" * 40,
            "ref": "refs/heads/main",
            "environment_id": None,
            "config_snapshot": snapshot,
            "snapshot_sha256": sha256_json(snapshot),
        }
        operation = await create_operation_request(
            session,
            kind=OperationKind.BUILD,
            requested_by="mcp",
            parameters=parameters,
            preview={"project": project.name},
        )
        await session.commit()
        operation_id = operation.id
        parameter_hash = operation.parameter_hash

    accepted = await client.post(
        f"/api/v1/approvals/{operation_id}/approve",
        headers=auth_headers,
        json={"parameter_hash": parameter_hash},
    )
    assert accepted.status_code == 200, accepted.text

    async with app.state.database.session_factory() as session:
        task = await session.scalar(
            select(RunnerTask).where(RunnerTask.operation_id == operation_id)
        )
        run = await session.get(PipelineRun, task.run_id if task else "")
        assert task is not None and run is not None
        assert task.kind == TaskKind.PIPELINE
        assert task.payload["snapshot_sha256"] == run.snapshot_sha256
        assert run.trigger_type == "mcp"


async def test_rollback_requires_and_uses_exact_previous_deployment(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    server = await _create_ready_server(
        client,
        auth_headers,
        name="rollback-node",
        host="10.0.0.30",
    )
    project = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "rollback-project",
            "repo_url": "https://example.com/rollback.git",
            "image_repository": "registry.example.com/rollback",
        },
    )
    environment = await client.post(
        f"/api/v1/projects/{project.json()['id']}/environments",
        headers=auth_headers,
        json={
            "name": "production",
            "server_id": server.json()["id"],
            "compose_source": "inline",
            "compose_content": "services:\n  app:\n    image: placeholder\n",
            "deploy_path": "/srv/rollback-project",
        },
    )
    assert environment.status_code == 201, environment.text
    compose = environment.json()["compose_content"]
    compose_sha256 = hashlib.sha256(compose.encode()).hexdigest()
    snapshot = {
        "id": environment.json()["id"],
        "project_id": project.json()["id"],
        "server_id": server.json()["id"],
        "name": "production",
        "deploy_path": "/srv/rollback-project",
        "compose_source": "inline",
        "compose_path": "compose.yaml",
        "compose_sha256": compose_sha256,
        "env_config": {},
        "healthcheck": environment.json()["healthcheck"],
        "registry_credential_id": None,
        "service_name": "app",
        "min_free_bytes": 512 * 1024 * 1024,
    }
    now = datetime.now(UTC)
    async with app.state.database.session_factory() as session:
        target = Deployment(
            project_id=project.json()["id"],
            environment_id=environment.json()["id"],
            server_id=server.json()["id"],
            status=DeploymentStatus.HEALTHY,
            image_ref="registry.example.com/rollback:target",
            image_digest="sha256:" + "a" * 64,
            revision="revision-1",
            compose_content=compose,
            compose_sha256=compose_sha256,
            environment_snapshot=snapshot,
            created_at=now - timedelta(minutes=3),
        )
        session.add(target)
        await session.flush()
        ambiguous = Deployment(
            project_id=project.json()["id"],
            environment_id=environment.json()["id"],
            server_id=server.json()["id"],
            status=DeploymentStatus.HEALTHY,
            image_ref="registry.example.com/rollback:wrong",
            image_digest="sha256:" + "b" * 64,
            revision="revision-1",
            compose_content=compose,
            compose_sha256=compose_sha256,
            environment_snapshot=snapshot,
            created_at=now - timedelta(minutes=2),
        )
        current = Deployment(
            project_id=project.json()["id"],
            environment_id=environment.json()["id"],
            server_id=server.json()["id"],
            status=DeploymentStatus.HEALTHY,
            image_ref="registry.example.com/rollback:current",
            image_digest="sha256:" + "c" * 64,
            revision="revision-2",
            previous_revision=target.revision,
            compose_content=compose,
            compose_sha256=compose_sha256,
            environment_snapshot=snapshot,
            created_at=now - timedelta(minutes=1),
        )
        session.add_all((ambiguous, current))
        await session.commit()
        identifiers = (target.id, ambiguous.id, current.id)

    missing_link = await client.post(
        f"/api/v1/deployments/{identifiers[2]}/rollback-request",
        headers=auth_headers,
    )
    assert missing_link.status_code == 409
    assert "exact previous deployment" in missing_link.json()["detail"]

    async with app.state.database.session_factory() as session:
        current = await session.get(Deployment, identifiers[2])
        assert current is not None
        current.previous_deployment_id = identifiers[0]
        await session.commit()

    requested = await client.post(
        f"/api/v1/deployments/{identifiers[2]}/rollback-request",
        headers=auth_headers,
    )
    assert requested.status_code == 202, requested.text
    approval = requested.json()
    assert approval["parameters"]["target_deployment_id"] == identifiers[0]
    assert approval["parameters"]["target_deployment_id"] != identifiers[1]
    assert approval["parameters"]["target_image_digest"] == "sha256:" + "a" * 64

    accepted = await client.post(
        f"/api/v1/approvals/{approval['id']}/approve",
        headers=auth_headers,
        json={"parameter_hash": approval["parameter_hash"]},
    )
    assert accepted.status_code == 200, accepted.text
    async with app.state.database.session_factory() as session:
        task = await session.scalar(
            select(RunnerTask).where(RunnerTask.operation_id == approval["id"])
        )
        assert task is not None
        assert task.payload["target_deployment_id"] == identifiers[0]
