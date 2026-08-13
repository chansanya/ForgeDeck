from __future__ import annotations

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from devops.domain.models import (
    Credential,
    DeploymentEnvironment,
    PipelineRun,
    Project,
    RunnerTask,
)
from devops.schemas import EnvironmentCreate, HealthCheckConfig, ProjectCreate
from devops.services import project_snapshot


async def test_health_login_and_auth(client: AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    denied = await client.get("/api/v1/projects")
    assert denied.status_code == 401

    bad = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert bad.status_code == 401

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"


async def test_credential_is_encrypted_and_never_returned(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={
            "name": "git-token",
            "kind": "git",
            "secret": "super-secret-value",
            "metadata": {"username": "ci"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "secret" not in body
    assert "encrypted_secret" not in body
    assert body["metadata"] == {"username": "ci"}

    async with app.state.database.session_factory() as session:
        credential = await session.scalar(select(Credential).where(Credential.id == body["id"]))
        assert credential is not None
        assert b"super-secret-value" not in credential.encrypted_secret
        assert (
            app.state.secret_manager.decrypt(credential.encrypted_secret)
            == "super-secret-value"
        )


async def test_project_trigger_freezes_snapshot_and_enqueues_task(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    registry = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={
            "name": "orders-registry",
            "kind": "registry",
            "secret": "registry-password",
            "metadata": {"username": "builder", "endpoint": "registry.example.com"},
        },
    )
    assert registry.status_code == 201, registry.text
    created = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "orders",
            "repo_url": "https://git.example.com/acme/orders.git",
            "default_branch": "main",
            "dockerfile_source": "inline",
            "dockerfile_content": "FROM eclipse-temurin:21-jre",
            "image_repository": "registry.example.com/acme/orders",
            "registry_credential_id": registry.json()["id"],
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    triggered = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers=auth_headers,
        json={"commit_sha": "a" * 40, "ref": "refs/heads/main"},
    )
    assert triggered.status_code == 202, triggered.text
    run_id = triggered.json()["id"]

    async with app.state.database.session_factory() as session:
        run = await session.get(PipelineRun, run_id)
        task = await session.scalar(select(RunnerTask).where(RunnerTask.run_id == run_id))
        assert run is not None and task is not None
        assert run.config_snapshot["project"]["repo_url"].endswith("orders.git")
        assert (
            run.config_snapshot["project"]["registry_credential_id"]
            == registry.json()["id"]
        )
        assert task.payload["snapshot_sha256"] == run.snapshot_sha256
        assert task.resource_key == f"project:{project_id}"


def test_environment_snapshot_binds_the_project_id() -> None:
    project = Project(
        id="project-1",
        name="orders",
        repo_url="https://example.com/orders.git",
        image_repository="registry.example.com/orders",
    )
    environment = DeploymentEnvironment(
        id="environment-1",
        project_id=project.id,
        server_id="server-1",
        name="production",
        deploy_path="/srv/orders",
    )

    snapshot = project_snapshot(project, environment)

    assert snapshot["environment"]["project_id"] == project.id


async def test_server_deploy_path_cannot_be_shared_between_environments(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    credential = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={"name": "deploy-path-ssh", "kind": "ssh", "secret": "password"},
    )
    assert credential.status_code == 201, credential.text
    server = await client.post(
        "/api/v1/servers",
        headers=auth_headers,
        json={
            "name": "deploy-path-server",
            "host": "deploy-path.example.test",
            "username": "deployer",
            "ssh_credential_id": credential.json()["id"],
            "host_key": "ssh-ed25519 AAAAC3NzaDeployPath",
        },
    )
    assert server.status_code == 201, server.text

    project_ids: list[str] = []
    for name in ("deploy-path-a", "deploy-path-b"):
        project = await client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={
                "name": name,
                "repo_url": f"https://example.com/{name}.git",
                "image_repository": f"registry.example.com/{name}",
            },
        )
        assert project.status_code == 201, project.text
        project_ids.append(project.json()["id"])

    first = await client.post(
        f"/api/v1/projects/{project_ids[0]}/environments",
        headers=auth_headers,
        json={
            "name": "production",
            "server_id": server.json()["id"],
            "deploy_path": "/srv/shared-target",
        },
    )
    assert first.status_code == 201, first.text
    conflict = await client.post(
        f"/api/v1/projects/{project_ids[1]}/environments",
        headers=auth_headers,
        json={
            "name": "production",
            "server_id": server.json()["id"],
            "deploy_path": "/srv/shared-target",
        },
    )

    assert conflict.status_code == 409
    assert "already assigned" in conflict.json()["detail"]


async def test_repository_path_traversal_is_rejected(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "unsafe",
            "repo_url": "https://example.com/unsafe.git",
            "dockerfile_path": "../../etc/passwd",
            "image_repository": "registry.example.com/unsafe",
        },
    )
    assert response.status_code == 422


async def test_sensitive_credential_metadata_is_rejected(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={
            "name": "leaky",
            "kind": "registry",
            "secret": "safe-secret-field",
            "metadata": {
                "nested": [
                    {"registry_password": "must-not-be-returned"},
                    {"githubToken": "also-secret"},
                ]
            },
        },
    )
    assert response.status_code == 422


async def test_git_remote_helper_and_embedded_http_credentials_are_rejected(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    for index, repo_url in enumerate(
        (
            "ext::sh -c touch /tmp/pwned",
            "https://user:password@example.com/repo.git",
            "ssh://git@example.com/repo.git",
            "git://example.com/repo.git",
            "git@example.com:team/repo.git",
        )
    ):
        response = await client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={
                "name": f"unsafe-repo-{index}",
                "repo_url": repo_url,
                "image_repository": "registry.example.com/unsafe",
            },
        )
        assert response.status_code == 422


def test_plaintext_secret_like_build_and_environment_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="plaintext secrets are not allowed"):
        ProjectCreate(
            name="unsafe-build-args",
            repo_url="https://example.com/repo.git",
            image_repository="registry.example.com/repo",
            build_args={"githubToken": "leaked"},
        )
    with pytest.raises(ValidationError, match="plaintext secrets are not allowed"):
        EnvironmentCreate(
            name="production",
            server_id="server-id",
            deploy_path="/srv/repo",
            env_config={"API_KEY": "leaked"},
        )


def test_environment_healthcheck_is_validated_before_persistence() -> None:
    valid = EnvironmentCreate(
        name="production",
        server_id="server-id",
        deploy_path="/srv/repo",
        healthcheck=HealthCheckConfig(
            kind="http",
            url="http://127.0.0.1:8080/health",
            status_min=200,
            status_max=299,
        ),
    )
    assert valid.healthcheck.kind == "http"

    with pytest.raises(ValidationError, match="TCP health checks require host and port"):
        EnvironmentCreate(
            name="production",
            server_id="server-id",
            deploy_path="/srv/repo",
            healthcheck=HealthCheckConfig(kind="tcp", host="db"),
        )
    with pytest.raises(ValidationError, match="argument array"):
        EnvironmentCreate(
            name="production",
            server_id="server-id",
            deploy_path="/srv/repo",
            healthcheck=HealthCheckConfig(kind="command", command=[]),
        )


async def test_short_commit_sha_is_rejected(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    project = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "full-sha-only",
            "repo_url": "https://example.com/full-sha.git",
            "image_repository": "registry.example.com/full-sha",
        },
    )
    response = await client.post(
        f"/api/v1/projects/{project.json()['id']}/runs",
        headers=auth_headers,
        json={"commit_sha": "abcdef0", "ref": "refs/heads/main"},
    )
    assert response.status_code == 422


async def test_template_catalog_returns_whitelisted_content(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/templates", headers=auth_headers)
    assert response.status_code == 200, response.text
    templates = response.json()
    assert {item["id"] for item in templates} == {
        "java-maven",
        "java-gradle",
        "node",
        "python",
    }
    assert all(
        any(line.startswith("FROM ") for line in item["dockerfile"].splitlines())
        for item in templates
    )
    assert all("services:" in item["compose"] for item in templates)
