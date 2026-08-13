from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx

from devops.runner.handlers import RunnerDependencies, _compose_project_name
from devops.runner.internal_api import create_internal_app
from devops.runner.process import AsyncCommandRunner
from devops.runner.ssh import AsyncSSHConnector, SSHCommandResult


async def test_internal_health_requires_bearer_token() -> None:
    dependencies = RunnerDependencies(
        session_factory=Mock(),
        workspace_dir=Path("."),
        secrets=Mock(),
        commands=AsyncCommandRunner(),
        ssh=AsyncSSHConnector(),
    )
    token = "t" * 32
    app = create_internal_app(dependencies=dependencies, token=token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        denied = await client.get("/internal/health")
        allowed = await client.get(
            "/internal/health", headers={"Authorization": f"Bearer {token}"}
        )

    assert denied.status_code == 401
    assert allowed.json() == {"status": "ok"}


class ScanConnector:
    async def scan_host_key(self, host: str, port: int):
        assert host == "node.example.test"
        assert port == 2222
        return SimpleNamespace(
            algorithm="ssh-ed25519",
            fingerprint="SHA256:candidate",
            public_key="ssh-ed25519 AAAAC3NzaCandidate",
        )


async def test_internal_host_key_scan_returns_candidate() -> None:
    dependencies = RunnerDependencies(
        session_factory=Mock(),
        workspace_dir=Path("."),
        secrets=Mock(),
        commands=AsyncCommandRunner(),
        ssh=ScanConnector(),  # type: ignore[arg-type]
    )
    token = "t" * 32
    app = create_internal_app(dependencies=dependencies, token=token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.post(
            "/internal/ssh/host-key/scan",
            headers={"Authorization": f"Bearer {token}"},
            json={"host": "node.example.test", "port": 2222},
        )

    assert response.status_code == 200, response.text
    assert response.json()["fingerprint"] == "SHA256:candidate"


class OverviewSession:
    async def run(self, argv: Any, **_: object) -> SSHCommandResult:
        values = tuple(argv)
        if values[:2] == ("docker", "version"):
            stdout = b'{"Version":"27"}'
        elif values[:3] == ("docker", "system", "df"):
            stdout = b'{"Type":"Images"}\n'
        else:
            stdout = b'{"Name":"item"}\n'
        return SSHCommandResult(values, 0, stdout, b"")


class OverviewConnector:
    @asynccontextmanager
    async def connect(self, config: object, credentials: object):
        yield OverviewSession()


async def test_internal_overview_returns_all_docker_resource_types(
    monkeypatch,
) -> None:
    async def load_target(
        *args: object, **kwargs: object
    ) -> tuple[object, object, tuple[str, ...]]:
        return object(), object(), ()

    monkeypatch.setattr("devops.runner.internal_api._load_ssh_target", load_target)
    dependencies = RunnerDependencies(
        session_factory=Mock(),
        workspace_dir=Path("."),
        secrets=Mock(),
        commands=AsyncCommandRunner(),
        ssh=OverviewConnector(),  # type: ignore[arg-type]
    )
    token = "t" * 32
    app = create_internal_app(dependencies=dependencies, token=token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.get(
            "/internal/servers/server-1/docker/overview",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert set(response.json()) == {
        "server_id",
        "version",
        "disk_usage",
        "containers",
        "images",
        "volumes",
        "networks",
    }


def test_compose_project_name_is_stable_and_environment_scoped() -> None:
    first = _compose_project_name("project-1", "environment-a")
    second = _compose_project_name("project-1", "environment-b")

    assert first == _compose_project_name("project-1", "environment-a")
    assert first != second
    assert len(first) <= 63
