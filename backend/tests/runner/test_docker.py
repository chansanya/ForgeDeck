from __future__ import annotations

import threading

import pytest

from devops.runner.docker import DockerSDKExecutor, RemoteDockerClient
from devops.runner.ssh import SSHCommandResult


class FakeSSHSession:
    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv: object, **_: object) -> SSHCommandResult:
        values = tuple(argv)  # type: ignore[arg-type]
        self.calls.append(values)
        return SSHCommandResult(values, 0, self.stdout, b"")


async def test_remote_docker_rejects_command_injection_in_container_name() -> None:
    session = FakeSSHSession()
    client = RemoteDockerClient(session)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid Docker container name"):
        await client.container_action("app; rm -rf /", "restart")
    assert not session.calls


async def test_remote_volume_removal_requires_exact_confirmation() -> None:
    session = FakeSSHSession()
    client = RemoteDockerClient(session)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly match"):
        await client.remove_volume("data", confirmation="DATA")
    assert not session.calls


async def test_remote_docker_lists_all_overview_resource_types() -> None:
    session = FakeSSHSession(b'{"Name":"resource"}\n')
    client = RemoteDockerClient(session)  # type: ignore[arg-type]

    assert (await client.list_images())[0]["Name"] == "resource"
    assert (await client.list_volumes())[0]["Name"] == "resource"
    assert (await client.list_networks())[0]["Name"] == "resource"

    commands = [call[:3] for call in session.calls]
    assert ("docker", "image", "ls") in commands
    assert ("docker", "volume", "ls") in commands
    assert ("docker", "network", "ls") in commands


async def test_remote_image_removal_checks_container_dependencies() -> None:
    session = FakeSSHSession(b"running-app\n")
    client = RemoteDockerClient(session)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="used by containers"):
        await client.remove_image("demo:1", confirmation="demo:1")

    assert all(call[:3] != ("docker", "image", "rm") for call in session.calls)


async def test_remote_compose_uses_scoped_docker_config() -> None:
    session = FakeSSHSession()
    client = RemoteDockerClient(
        session,  # type: ignore[arg-type]
        docker_config="/tmp/light-devops-docker-config-test",
    )

    await client.compose_up(
        project_name="demo",
        project_directory="/srv/demo",
        files=("/srv/demo/compose.yaml",),
        wait=False,
    )

    assert session.calls[0][:4] == (
        "env",
        "DOCKER_CONFIG=/tmp/light-devops-docker-config-test",
        "docker",
        "compose",
    )


class FakeDockerClient:
    def __init__(self) -> None:
        self.thread_id: int | None = None
        self.closed = False

    def ping(self) -> bool:
        self.thread_id = threading.get_ident()
        return True

    def close(self) -> None:
        self.closed = True


async def test_docker_sdk_runs_outside_event_loop_thread() -> None:
    client = FakeDockerClient()
    executor = DockerSDKExecutor(client_factory=lambda: client, max_workers=1)
    event_loop_thread = threading.get_ident()
    assert await executor.ping()
    await executor.aclose()
    assert client.thread_id is not None
    assert client.thread_id != event_loop_thread
    assert client.closed
