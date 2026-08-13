from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select
from starlette.responses import JSONResponse

from devops.config import Settings
from devops.domain.models import (
    Credential,
    CredentialKind,
    Deployment,
    DeploymentEnvironment,
    DeploymentStatus,
    MCPAccessToken,
    OperationKind,
    OperationRequest,
    Project,
    Server,
    utcnow,
)
from devops.integrations.mcp import (
    BearerAuthMiddleware,
    _current_mcp_actor,
    _docker_overview_view,
    _reject_sensitive_parameter_keys,
    _required_scopes,
    _scope_granted,
    create_mcp_server,
)


async def _ok_app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            break
    await JSONResponse({"ok": True})(scope, receive, send)


async def _create_raw_token(app, scopes: list[str], *, expired: bool = False) -> str:
    raw = secrets.token_urlsafe(32)
    async with app.state.database.session_factory() as session:
        session.add(
            MCPAccessToken(
                name=f"test-{secrets.token_hex(4)}",
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                scopes=scopes,
                expires_at=utcnow()
                + (timedelta(seconds=-1) if expired else timedelta(hours=1)),
            )
        )
        await session.commit()
    return raw


async def _middleware_client(
    app, *, bootstrap_token: str | None = None, target=_ok_app
) -> AsyncClient:
    middleware = BearerAuthMiddleware(
        target,
        app.state.database,
        bootstrap_token=bootstrap_token,
    )
    return AsyncClient(transport=ASGITransport(app=middleware), base_url="http://mcp")


def _tool_call(name: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


async def _create_rollback_chain(
    app,
    *,
    target_deploy_path: str = "/srv/devops/app",
    current_deploy_path: str = "/srv/devops/app",
    add_newer_deployment: bool = False,
) -> tuple[str, str, dict[str, object]]:
    suffix = secrets.token_hex(4)
    target_compose = "services:\n  app:\n    image: previous\n"
    current_compose = "services:\n  app:\n    image: current\n"
    target_created_at = utcnow() - timedelta(minutes=3)
    current_created_at = utcnow() - timedelta(minutes=2)
    target_snapshot: dict[str, object] = {
        "id": f"environment-{suffix}",
        "project_id": f"project-{suffix}",
        "server_id": f"server-{suffix}",
        "name": "production",
        "deploy_path": target_deploy_path,
        "env_config": {"RELEASE": "previous"},
        "healthcheck": {"type": "http", "url": "http://previous/health"},
        "registry_credential_id": None,
        "service_name": "app",
        "min_free_bytes": 1024,
        "target_only": "must-be-preserved",
    }
    current_snapshot: dict[str, object] = {
        "id": f"environment-{suffix}",
        "project_id": f"project-{suffix}",
        "server_id": f"server-{suffix}",
        "name": "production",
        "deploy_path": current_deploy_path,
        "env_config": {"RELEASE": "current"},
        "healthcheck": {"type": "http", "url": "http://current/health"},
        "registry_credential_id": None,
        "service_name": "app",
        "min_free_bytes": 2048,
        "current_only": "must-not-be-copied",
    }
    async with app.state.database.session_factory() as session:
        credential = Credential(
            id=f"credential-{suffix}",
            name=f"ssh-{suffix}",
            kind=CredentialKind.SSH,
            encrypted_secret=b"encrypted-test-secret",
        )
        server = Server(
            id=f"server-{suffix}",
            name=f"server-{suffix}",
            host="127.0.0.1",
            username="devops",
            ssh_credential_id=credential.id,
            host_key="ssh-ed25519 AAAATEST",
        )
        project = Project(
            id=f"project-{suffix}",
            name=f"project-{suffix}",
            repo_url="https://example.test/repository.git",
            image_repository="registry.example.test/app",
        )
        environment = DeploymentEnvironment(
            id=f"environment-{suffix}",
            project_id=project.id,
            server_id=server.id,
            name="production",
            deploy_path=current_deploy_path,
        )
        session.add_all([credential, server, project, environment])
        await session.flush()

        target = Deployment(
            id=f"deployment-previous-{suffix}",
            project_id=project.id,
            environment_id=environment.id,
            server_id=server.id,
            status=DeploymentStatus.HEALTHY,
            image_ref="registry.example.test/app",
            image_digest="sha256:" + "a" * 64,
            revision="a" * 40,
            compose_content=target_compose,
            compose_sha256=hashlib.sha256(target_compose.encode()).hexdigest(),
            environment_snapshot=target_snapshot,
            created_at=target_created_at,
        )
        session.add(target)
        await session.flush()

        current = Deployment(
            id=f"deployment-current-{suffix}",
            project_id=project.id,
            environment_id=environment.id,
            server_id=server.id,
            status=DeploymentStatus.HEALTHY,
            image_ref="registry.example.test/app",
            image_digest="sha256:" + "b" * 64,
            revision="b" * 40,
            previous_revision=target.revision,
            previous_deployment_id=target.id,
            compose_content=current_compose,
            compose_sha256=hashlib.sha256(current_compose.encode()).hexdigest(),
            environment_snapshot=current_snapshot,
            created_at=current_created_at,
        )
        session.add(current)
        await session.flush()

        if add_newer_deployment:
            session.add(
                Deployment(
                    id=f"deployment-newer-{suffix}",
                    project_id=project.id,
                    environment_id=environment.id,
                    server_id=server.id,
                    status=DeploymentStatus.HEALTHY,
                    image_ref="registry.example.test/app",
                    image_digest="sha256:" + "c" * 64,
                    revision="c" * 40,
                    previous_revision=current.revision,
                    previous_deployment_id=current.id,
                    compose_content=current_compose,
                    compose_sha256=hashlib.sha256(current_compose.encode()).hexdigest(),
                    environment_snapshot=current_snapshot,
                    created_at=utcnow() - timedelta(minutes=1),
                )
            )
        await session.commit()
    return current.id, target.id, target_snapshot


def test_mcp_scope_mapping_is_explicit_and_legacy_families_remain_supported() -> None:
    assert _required_scopes(str(_tool_call("request_pipeline")).encode()) == set()
    assert _required_scopes(
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"request_pipeline"}}'
    ) == {"request:build"}
    assert _scope_granted({"request:build"}, "request:build")
    assert _scope_granted({"request"}, "request:deploy")
    assert _scope_granted({"*"}, "read:logs")
    assert not _scope_granted({"read:status"}, "read:logs")
    assert _required_scopes(
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"deploy_now"}}'
    ) == {"tool:unsupported"}
    assert not _scope_granted({"read:status", "request"}, "tool:unsupported")
    assert not _scope_granted({"*"}, "tool:unsupported")


def test_mcp_docker_overview_uses_a_strict_safe_field_allowlist() -> None:
    overview = _docker_overview_view(
        "server-1",
        {
            "server_id": "spoofed-server",
            "version": {
                "Version": "27.5.1",
                "ApiVersion": "1.47",
                "Components": [{"Details": {"Env": ["TOKEN=version-secret"]}}],
                "RegistryConfig": {"Mirrors": ["https://user:pass@example.test"]},
            },
            "disk_usage": [
                {
                    "Type": "Images",
                    "Size": "1.2GB",
                    "Reclaimable": "500MB (41%)",
                    "Labels": "disk-secret",
                }
            ],
            "containers": [
                {
                    "ID": "container-id",
                    "Names": "api",
                    "Image": "registry.example.test/api@sha256:abc",
                    "State": "running",
                    "Status": "Up 2 hours (healthy)",
                    "Ports": "127.0.0.1:8080->8080/tcp",
                    "Command": "server --token container-secret",
                    "Labels": "api.token=container-secret",
                    "Env": ["PASSWORD=container-secret"],
                    "Mounts": "/run/secrets/registry-password",
                }
            ],
            "images": [
                {
                    "ID": "image-id",
                    "Repository": "registry.example.test/api",
                    "Tag": "latest",
                    "Digest": "sha256:abc",
                    "Labels": "registry.password=image-secret",
                }
            ],
            "volumes": [
                {
                    "Name": "app-data",
                    "Driver": "local",
                    "Mountpoint": "/var/lib/docker/volumes/app-data/_data",
                    "Labels": "backup.token=volume-secret",
                }
            ],
            "networks": [
                {
                    "ID": "network-id",
                    "Name": "app-network",
                    "Driver": "bridge",
                    "Scope": "local",
                    "Labels": "auth.secret=network-secret",
                }
            ],
        },
    )

    assert overview == {
        "server_id": "server-1",
        "version": {"Version": "27.5.1", "ApiVersion": "1.47"},
        "disk_usage": [
            {"Type": "Images", "Size": "1.2GB", "Reclaimable": "500MB (41%)"}
        ],
        "containers": [
            {
                "ID": "container-id",
                "Names": "api",
                "Image": "registry.example.test/api@sha256:abc",
                "State": "running",
                "Status": "Up 2 hours (healthy)",
                "Ports": "127.0.0.1:8080->8080/tcp",
            }
        ],
        "images": [
            {
                "ID": "image-id",
                "Repository": "registry.example.test/api",
                "Tag": "latest",
                "Digest": "sha256:abc",
            }
        ],
        "volumes": [{"Name": "app-data", "Driver": "local"}],
        "networks": [
            {
                "ID": "network-id",
                "Name": "app-network",
                "Driver": "bridge",
                "Scope": "local",
            }
        ],
    }
    serialized = json.dumps(overview)
    assert "Command" not in serialized
    assert "Labels" not in serialized
    assert "Mountpoint" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    "arguments",
    [
        {"apiToken": "value"},
        {"client-secret": "value"},
        {"outer": {"registry.password": "value"}},
        {"items": [{"sshPrivateKey": "value"}]},
        {"ＰＡＳＳＷＯＲＤ": "value"},
        {"authorization_header": "value"},
    ],
)
def test_mcp_script_arguments_reject_nested_and_variant_secret_keys(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="looks secret-bearing"):
        _reject_sensitive_parameter_keys(arguments, field_name="arguments")


async def test_mcp_rollback_uses_exact_previous_deployment_snapshot(app) -> None:
    current_id, target_id, target_snapshot = await _create_rollback_chain(app)
    mcp = create_mcp_server(app.state.database)

    await mcp.call_tool("request_rollback", {"deployment_id": current_id})

    async with app.state.database.session_factory() as session:
        request = await session.scalar(
            select(OperationRequest).where(OperationRequest.kind == OperationKind.ROLLBACK)
        )
    assert request is not None
    assert request.parameters["deployment_id"] == current_id
    assert request.parameters["target_deployment_id"] == target_id
    assert request.parameters["environment_snapshot"] == target_snapshot
    assert "current_only" not in request.parameters["environment_snapshot"]


async def test_mcp_rollback_rejects_non_active_healthy_deployment(app) -> None:
    current_id, _, _ = await _create_rollback_chain(app, add_newer_deployment=True)
    mcp = create_mcp_server(app.state.database)

    with pytest.raises(ToolError, match="active healthy deployment"):
        await mcp.call_tool("request_rollback", {"deployment_id": current_id})

    async with app.state.database.session_factory() as session:
        assert await session.scalar(select(OperationRequest.id).limit(1)) is None


async def test_mcp_rollback_rejects_deploy_path_drift(app) -> None:
    current_id, _, _ = await _create_rollback_chain(
        app,
        target_deploy_path="/srv/devops/previous-path",
        current_deploy_path="/srv/devops/current-path",
    )
    mcp = create_mcp_server(app.state.database)

    with pytest.raises(ToolError, match="inconsistent with the active target"):
        await mcp.call_tool("request_rollback", {"deployment_id": current_id})

    async with app.state.database.session_factory() as session:
        assert await session.scalar(select(OperationRequest.id).limit(1)) is None


def test_production_rejects_permanent_mcp_bootstrap_token() -> None:
    with pytest.raises(ValueError, match="bootstrap access is disabled in production"):
        Settings(
            environment="production",
            internal_token="internal-" + "i" * 32,
            mcp_token="bootstrap-" + "m" * 32,
        )


async def test_mcp_bootstrap_token_is_read_only(app) -> None:
    bootstrap_token = "bootstrap-" + "b" * 32
    async with await _middleware_client(app, bootstrap_token=bootstrap_token) as client:
        allowed_status = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {bootstrap_token}"},
            json=_tool_call("list_servers"),
        )
        denied_logs = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {bootstrap_token}"},
            json=_tool_call("tail_pipeline_logs"),
        )
        denied_write = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {bootstrap_token}"},
            json=_tool_call("request_pipeline"),
        )

    assert allowed_status.status_code == 200
    assert denied_logs.status_code == 403
    assert denied_write.status_code == 403


async def test_database_mcp_token_propagates_auditable_identity(app) -> None:
    raw_token = await _create_raw_token(app, ["read:status"])

    async def identity_app(scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await JSONResponse({"actor": _current_mcp_actor()})(scope, receive, send)

    async with await _middleware_client(app, target=identity_app) as client:
        response = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {raw_token}"},
            json=_tool_call("list_servers"),
        )

    assert response.status_code == 200
    assert response.json()["actor"].startswith("mcp-token:")


async def test_mcp_token_scopes_are_enforced_per_tool(app) -> None:
    status_token = await _create_raw_token(app, ["read:status"])
    build_token = await _create_raw_token(app, ["request:build"])
    logs_token = await _create_raw_token(app, ["read:logs"])

    async with await _middleware_client(app) as client:
        allowed_status = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {status_token}"},
            json=_tool_call("list_servers"),
        )
        denied_write = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {status_token}"},
            json=_tool_call("request_pipeline"),
        )
        allowed_build = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {build_token}"},
            json=_tool_call("request_pipeline"),
        )
        denied_deploy = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {build_token}"},
            json=_tool_call("request_deployment"),
        )
        allowed_logs = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {logs_token}"},
            json=_tool_call("tail_pipeline_logs"),
        )

    assert allowed_status.status_code == 200
    assert denied_write.status_code == 403
    assert allowed_build.status_code == 200
    assert denied_deploy.status_code == 403
    assert allowed_logs.status_code == 200


async def test_mcp_expired_and_revoked_tokens_are_rejected(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    expired = await _create_raw_token(app, ["read:status"], expired=True)
    created = await client.post(
        "/api/v1/mcp/tokens",
        headers=auth_headers,
        json={"name": "revocable", "scopes": ["read:status"], "expires_in_seconds": 3600},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scopes"] == ["read:status"]

    revoked = await client.delete(
        f"/api/v1/mcp/tokens/{body['id']}", headers=auth_headers
    )
    assert revoked.status_code == 204

    async with await _middleware_client(app) as middleware_client:
        expired_response = await middleware_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {expired}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        revoked_response = await middleware_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {body['token']}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert expired_response.status_code == 401
    assert revoked_response.status_code == 401


async def test_mcp_body_limit_and_canonical_route(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/mcp/tokens",
        headers=auth_headers,
        json={"name": "body-limit", "scopes": ["read:status"], "expires_in_seconds": 3600},
    )
    token = created.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    exact = await client.post("/mcp", headers={"Content-Type": "application/json"}, content=b"{}")
    slash = await client.post("/mcp/", headers={"Content-Type": "application/json"}, content=b"{}")
    oversized = await client.post("/mcp", headers=headers, content=b"x" * (1024 * 1024 + 1))
    discovery = await client.get("/.well-known/oauth-protected-resource")
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
    protocol_headers = {
        **headers,
        "Accept": "application/json, text/event-stream",
        "Host": "devops.test",
    }
    initialized = await client.post("/mcp", headers=protocol_headers, json=initialize)
    invalid_host = await client.post(
        "/mcp",
        headers={**protocol_headers, "Host": "evil.example"},
        json=initialize,
    )

    assert exact.status_code == 401
    assert slash.status_code == 401
    assert oversized.status_code == 413
    assert discovery.status_code == 404
    assert initialized.status_code == 200, initialized.text
    assert initialized.json()["result"]["serverInfo"]["name"] == "Light DevOps"
    assert invalid_host.status_code == 421
