from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from devops.runner.credentials import RegistryCredentials
from devops.runner.deploy import (
    ComposeDeployer,
    DeploymentError,
    DeploymentRequest,
    HealthChecker,
    HealthCheckKind,
    HealthCheckSpec,
    _docker_config_for_request,
    cleanup_stale_docker_configs,
)
from devops.runner.ssh import SSHCommandResult

DIGEST = "sha256:" + "b" * 64


class FakeSession:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.commands: list[tuple[str, ...]] = []
        self.available_blocks = 2 * 1024 * 1024
        self.symlinks: set[str] = set()
        self.stale_docker_configs: tuple[str, ...] = ()
        self.missing_executables: set[str] = set()

    async def exists(self, path: str) -> bool:
        return path in self.files

    async def read_file(self, path: str, **_: object) -> bytes:
        return self.files[path]

    async def write_file_atomic(self, path: str, data: bytes, **_: object) -> None:
        self.files[path] = data

    async def remove_file(self, path: str) -> None:
        self.files.pop(path, None)

    async def run(self, argv, **_: object) -> SSHCommandResult:
        values = tuple(argv)
        self.commands.append(values)
        if values and values[0] in self.missing_executables:
            return SSHCommandResult(values, 127, b"", b"command not found")
        if values[:2] == ("df", "-Pk"):
            stdout = (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/test 4000000 1 {self.available_blocks} 1% /srv\n"
            ).encode()
        elif values[:2] == ("realpath", "-m"):
            stdout = f"{values[-1]}\n".encode()
        elif values and values[0] == "find" and values[1] == "/tmp":
            stdout = ("\n".join(self.stale_docker_configs) + "\n").encode()
        elif values and values[0] == "find" and values[1] in self.symlinks:
            stdout = f"{values[1]}\n".encode()
        elif values[:2] == ("id", "-u"):
            stdout = b"1000\n"
        elif values and values[0] == "curl":
            stdout = b"204"
        else:
            stdout = b""
        return SSHCommandResult(values, 0, stdout, b"")


class FakeDocker:
    instances: list[FakeDocker] = []
    fail_first = False

    def __init__(
        self, session: FakeSession, *, docker_config: str | None = None
    ) -> None:
        self.session = session
        self.docker_config = docker_config
        self.up_calls = 0
        self.compose_up_calls: list[dict[str, object]] = []
        self.compose_down_calls: list[dict[str, object]] = []
        self.auth_seen = False
        self.config_content_seen: bytes | None = None
        self.__class__.instances.append(self)

    async def version(self) -> dict[str, str]:
        return {"Version": "28"}

    async def compose_version(self) -> str:
        return "2.39.0"

    async def compose_ps(self, **_: object) -> tuple[object, ...]:
        return ()

    async def compose_up(self, **kwargs: object) -> None:
        self.up_calls += 1
        self.compose_up_calls.append(dict(kwargs))
        if self.docker_config is not None:
            config_path = f"{self.docker_config}/config.json"
            self.auth_seen = config_path in self.session.files
            self.config_content_seen = self.session.files.get(config_path)
        if self.fail_first and self.up_calls == 1:
            raise RuntimeError("new revision failed")

    async def compose_down(self, **kwargs: object) -> None:
        self.compose_down_calls.append(dict(kwargs))


class Healthy(HealthChecker):
    async def wait_until_healthy(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"kind": "compose", "services": 1}


def request(
    *,
    registry: bool = False,
    health_check: HealthCheckSpec | None = None,
    env_content: bytes | None = None,
) -> DeploymentRequest:
    return DeploymentRequest(
        project_name="demo",
        service_name="app",
        remote_directory="/srv/demo",
        compose_content=b"services: {app: {}}\n",
        image_ref="registry.example.test/demo:latest",
        image_digest=DIGEST,
        revision="rev-2",
        env_content=env_content,
        registry_credentials=(
            RegistryCredentials("builder", "registry-password", "registry.example.test")
            if registry
            else None
        ),
        health_check=health_check
        or HealthCheckSpec(timeout_seconds=2, interval_seconds=0.01),
    )


def test_default_compose_template_has_safe_interpolation_and_no_wget_healthcheck() -> None:
    template = (
        Path(__file__).resolve().parents[3] / "templates" / "compose" / "compose.yaml"
    ).read_text(encoding="utf-8")

    assert "image: ${APP_IMAGE:-" in template
    assert "${APP_IMAGE:?" not in template
    assert "env_file:" in template
    assert "healthcheck:" not in template
    assert "wget" not in template


async def test_compose_deploy_pins_digest_and_writes_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    session = FakeSession()

    result = await ComposeDeployer(health_checker=Healthy()).deploy(
        request(registry=True), session=session  # type: ignore[arg-type]
    )

    override = json.loads(session.files["/srv/demo/compose.devops.json"])
    assert override["services"]["app"]["image"].endswith(f"@{DIGEST}")
    revision = json.loads(session.files["/srv/demo/.devops/revision.json"])
    assert revision["revision"] == "rev-2"
    assert not result.rolled_back
    assert FakeDocker.instances[-1].auth_seen
    assert not any("docker-config" in path for path in session.files)


async def test_deploy_uses_an_empty_scoped_docker_config_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)

    await ComposeDeployer(health_checker=Healthy()).deploy(
        request(), session=FakeSession()  # type: ignore[arg-type]
    )

    docker = FakeDocker.instances[-1]
    assert docker.docker_config is not None
    assert docker.config_content_seen == b'{"auths":{}}'


def test_deployment_docker_config_contains_current_and_rollback_registries() -> None:
    deployment = request()
    merged = DeploymentRequest(
        project_name=deployment.project_name,
        service_name=deployment.service_name,
        remote_directory=deployment.remote_directory,
        compose_content=deployment.compose_content,
        image_ref=deployment.image_ref,
        image_digest=deployment.image_digest,
        revision=deployment.revision,
        registry_credentials=RegistryCredentials(
            "current-user", "current-password", "registry.example.test"
        ),
        rollback_registry_credentials=RegistryCredentials(
            "rollback-user", "rollback-password", "old-registry.example.test"
        ),
        rollback_image_ref="old-registry.example.test/demo:previous",
    )

    config = json.loads(_docker_config_for_request(merged))

    assert set(config["auths"]) == {
        "registry.example.test",
        "old-registry.example.test",
    }


async def test_new_deploy_atomically_replaces_stale_env_and_always_passes_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    session = FakeSession({"/srv/demo/.env": b"STALE_VALUE=1\n"})

    await ComposeDeployer(health_checker=Healthy()).deploy(
        request(), session=session  # type: ignore[arg-type]
    )

    assert session.files["/srv/demo/.env"] == b""
    docker = FakeDocker.instances[-1]
    assert [call["env_file"] for call in docker.compose_up_calls] == [
        "/srv/demo/.env"
    ]


async def test_failed_deploy_restores_previous_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = True
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    previous = {
        "/srv/demo/compose.yaml": b"old-compose",
        "/srv/demo/compose.devops.json": b"old-override",
        "/srv/demo/.env": b"OLD=1\n",
        "/srv/demo/.devops/revision.json": b'{"revision":"rev-1"}',
    }
    session = FakeSession(previous)

    with pytest.raises(DeploymentError) as caught:
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert caught.value.rolled_back
    assert session.files == previous
    docker = FakeDocker.instances[-1]
    assert docker.up_calls == 2
    assert [call["env_file"] for call in docker.compose_up_calls] == [
        "/srv/demo/.env",
        "/srv/demo/.env",
    ]


async def test_failed_deploy_restores_absent_env_snapshot_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = True
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    previous = {
        "/srv/demo/compose.yaml": b"old-compose",
        "/srv/demo/compose.devops.json": b"old-override",
        "/srv/demo/.devops/revision.json": b'{"revision":"rev-1"}',
    }
    session = FakeSession(previous)

    with pytest.raises(DeploymentError) as caught:
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(env_content=b"NEW_VALUE=1\n"),
            session=session,  # type: ignore[arg-type]
        )

    assert caught.value.rolled_back
    assert session.files == previous
    docker = FakeDocker.instances[-1]
    assert [call["env_file"] for call in docker.compose_up_calls] == [
        "/srv/demo/.env",
        None,
    ]


async def test_hard_crash_recovers_persisted_previous_snapshot_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HardCrash(BaseException):
        pass

    class CrashOnceDocker(FakeDocker):
        crashed = False
        compose_snapshots: list[bytes | None] = []

        async def compose_up(self, **kwargs: object) -> None:
            self.up_calls += 1
            self.compose_up_calls.append(dict(kwargs))
            self.__class__.compose_snapshots.append(
                self.session.files.get("/srv/demo/compose.yaml")
            )
            if not self.__class__.crashed:
                self.__class__.crashed = True
                raise HardCrash("runner process disappeared")

    FakeDocker.instances.clear()
    CrashOnceDocker.crashed = False
    CrashOnceDocker.compose_snapshots.clear()
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", CrashOnceDocker)
    previous = {
        "/srv/demo/compose.yaml": b"old-compose",
        "/srv/demo/compose.devops.json": b"old-override",
        "/srv/demo/.env": b"OLD=1\n",
        "/srv/demo/.devops/revision.json": b'{"revision":"rev-1"}',
    }
    session = FakeSession(previous)
    deployer = ComposeDeployer(health_checker=Healthy())

    with pytest.raises(HardCrash):
        await deployer.deploy(request(), session=session)  # type: ignore[arg-type]

    assert "/srv/demo/.devops/pending.json" in session.files
    assert session.files["/srv/demo/compose.yaml"] == request().compose_content

    result = await deployer.deploy(request(), session=session)  # type: ignore[arg-type]

    assert result.revision == "rev-2"
    assert "/srv/demo/.devops/pending.json" not in session.files
    assert CrashOnceDocker.compose_snapshots[-2:] == [
        b"old-compose",
        request().compose_content,
    ]


async def test_cancellation_after_control_file_write_restores_previous_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelOnceDocker(FakeDocker):
        async def compose_up(self, **kwargs: object) -> None:
            self.up_calls += 1
            self.compose_up_calls.append(dict(kwargs))
            if self.up_calls == 1:
                raise asyncio.CancelledError

    FakeDocker.instances.clear()
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", CancelOnceDocker)
    previous = {
        "/srv/demo/compose.yaml": b"old-compose",
        "/srv/demo/compose.devops.json": b"old-override",
        "/srv/demo/.devops/revision.json": b'{"revision":"rev-1"}',
    }
    session = FakeSession(previous)

    with pytest.raises(DeploymentError, match="cancelled") as caught:
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert caught.value.rolled_back
    assert session.files == previous
    assert CancelOnceDocker.instances[-1].up_calls == 2


async def test_failed_first_deploy_does_not_claim_rollback_without_success_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = True
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    previous = {
        "/srv/demo/compose.yaml": b"unverified-compose",
        "/srv/demo/compose.devops.json": b"unverified-override",
    }
    session = FakeSession(previous)

    with pytest.raises(DeploymentError) as caught:
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert not caught.value.rolled_back
    assert session.files == previous
    assert len(FakeDocker.instances[-1].compose_down_calls) == 1


async def test_preflight_disk_failure_does_not_overwrite_deployment_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    previous = {"/srv/demo/compose.yaml": b"old-compose"}
    session = FakeSession(previous)
    session.available_blocks = 1

    with pytest.raises(DeploymentError, match="insufficient disk space"):
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert session.files == previous


async def test_preflight_compose_failure_does_not_overwrite_deployment_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoComposeDocker(FakeDocker):
        async def compose_version(self) -> str:
            raise RuntimeError("docker compose is unavailable")

    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", NoComposeDocker)
    previous = {"/srv/demo/compose.yaml": b"old-compose"}
    session = FakeSession(previous)

    with pytest.raises(DeploymentError, match="compose is unavailable"):
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert session.files == previous


async def test_preflight_rejects_compose_older_than_2_20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OldComposeDocker(FakeDocker):
        async def compose_version(self) -> str:
            return "2.19.9"

    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", OldComposeDocker)
    previous = {"/srv/demo/compose.yaml": b"old-compose"}
    session = FakeSession(previous)

    with pytest.raises(DeploymentError, match="Compose 2.20.0 or newer"):
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert session.files == previous
    assert FakeDocker.instances[-1].up_calls == 0


async def test_preflight_accepts_v_prefixed_minimum_compose_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MinimumComposeDocker(FakeDocker):
        async def compose_version(self) -> str:
            return "v2.20.0"

    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", MinimumComposeDocker)

    await ComposeDeployer(health_checker=Healthy()).deploy(
        request(), session=FakeSession()  # type: ignore[arg-type]
    )

    assert FakeDocker.instances[-1].up_calls == 1


@pytest.mark.parametrize(
    ("health_check", "executable", "probe"),
    (
        (
            HealthCheckSpec(
                kind=HealthCheckKind.HTTP,
                url="http://127.0.0.1:8080/health",
                timeout_seconds=2,
                interval_seconds=0.01,
            ),
            "curl",
            ("curl", "--version"),
        ),
        (
            HealthCheckSpec(
                kind=HealthCheckKind.TCP,
                host="127.0.0.1",
                port=8080,
                timeout_seconds=2,
                interval_seconds=0.01,
            ),
            "nc",
            ("nc", "-h"),
        ),
    ),
    ids=("http-curl", "tcp-netcat"),
)
async def test_preflight_requires_configured_health_check_tool_before_file_writes(
    monkeypatch: pytest.MonkeyPatch,
    health_check: HealthCheckSpec,
    executable: str,
    probe: tuple[str, ...],
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    previous = {"/srv/demo/compose.yaml": b"old-compose"}
    session = FakeSession(previous)
    session.missing_executables.add(executable)

    with pytest.raises(DeploymentError, match=rf"{executable} is required"):
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(health_check=health_check),
            session=session,  # type: ignore[arg-type]
        )

    assert probe in session.commands
    assert session.files == previous
    assert FakeDocker.instances[-1].up_calls == 0


async def test_reconcile_skips_repeating_an_already_healthy_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReconciledDocker(FakeDocker):
        async def compose_ps(self, **_: object) -> tuple[dict[str, str], ...]:
            return ({"Service": "app", "Name": "demo-app-1"},)

        async def inspect_container(self, name: str) -> dict[str, object]:
            assert name == "demo-app-1"
            return {
                "Config": {
                    "Image": f"registry.example.test/demo@{DIGEST}",
                    "Labels": {"devops.revision": "rev-2"},
                },
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
            }

    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", ReconciledDocker)
    session = FakeSession(
        {
            "/srv/demo/compose.yaml": b"existing-compose",
            "/srv/demo/compose.devops.json": b"existing-override",
        }
    )

    result = await ComposeDeployer(health_checker=Healthy()).deploy(
        request(), session=session  # type: ignore[arg-type]
    )

    assert result.health["kind"] == "reconciled"
    assert ReconciledDocker.instances[-1].up_calls == 0
    revision = json.loads(session.files["/srv/demo/.devops/revision.json"])
    assert revision["revision"] == "rev-2"


async def test_reconcile_requires_compose_and_configured_health_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReconciledDocker(FakeDocker):
        async def compose_ps(self, **_: object) -> tuple[dict[str, str], ...]:
            return (
                {"Service": "app", "Name": "demo-app-1"},
                {"Service": "worker", "Name": "demo-worker-1"},
            )

        async def inspect_container(self, name: str) -> dict[str, object]:
            assert name == "demo-app-1"
            return {
                "Config": {
                    "Image": f"registry.example.test/demo@{DIGEST}",
                    "Labels": {"devops.revision": "rev-2"},
                },
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
            }

    class RecordingHealth:
        def __init__(self) -> None:
            self.kinds: list[HealthCheckKind] = []

        async def wait_until_healthy(
            self, spec: HealthCheckSpec, **_: object
        ) -> dict[str, object]:
            self.kinds.append(spec.kind)
            return {"kind": spec.kind.value}

    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", ReconciledDocker)
    checker = RecordingHealth()
    session = FakeSession(
        {
            "/srv/demo/compose.yaml": b"existing-compose",
            "/srv/demo/compose.devops.json": b"existing-override",
        }
    )

    result = await ComposeDeployer(health_checker=checker).deploy(
        request(
            health_check=HealthCheckSpec(
                kind=HealthCheckKind.HTTP,
                url="http://127.0.0.1:8080/health",
                timeout_seconds=2,
                interval_seconds=0.01,
            )
        ),
        session=session,  # type: ignore[arg-type]
    )

    assert checker.kinds == [HealthCheckKind.COMPOSE, HealthCheckKind.HTTP]
    assert result.health["kind"] == "reconciled"
    assert result.health["compose"] == {"kind": "compose"}
    assert result.health["configured"] == {"kind": "http"}
    assert ReconciledDocker.instances[-1].up_calls == 0


async def test_preflight_rejects_symlinked_devops_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    session = FakeSession({"/srv/demo/compose.yaml": b"old-compose"})
    session.symlinks.add("/srv/demo/.devops")

    with pytest.raises(DeploymentError, match="symlinked deployment control path"):
        await ComposeDeployer(health_checker=Healthy()).deploy(
            request(), session=session  # type: ignore[arg-type]
        )

    assert session.files["/srv/demo/compose.yaml"] == b"old-compose"


async def test_next_connection_removes_only_valid_stale_docker_config_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDocker.instances.clear()
    FakeDocker.fail_first = False
    monkeypatch.setattr("devops.runner.deploy.RemoteDockerClient", FakeDocker)
    session = FakeSession()
    stale = "/tmp/light-devops-docker-config-" + "a" * 32
    session.stale_docker_configs = (
        stale,
        "/tmp/light-devops-docker-config-not-a-managed-directory",
    )

    await ComposeDeployer(health_checker=Healthy()).deploy(
        request(), session=session  # type: ignore[arg-type]
    )

    removals = [command for command in session.commands if command[:3] == ("rm", "-rf", "--")]
    assert removals == [("rm", "-rf", "--", stale)]


async def test_http_and_tcp_health_checks_execute_on_target_session() -> None:
    session = FakeSession()
    checker = HealthChecker()
    docker = FakeDocker(session)

    http = await checker._check_once(
        HealthCheckSpec(
            kind=HealthCheckKind.HTTP,
            url="http://127.0.0.1:8080/health",
            interval_seconds=1,
        ),
        session=session,  # type: ignore[arg-type]
        docker=docker,  # type: ignore[arg-type]
        project_name="demo",
        project_directory="/srv/demo",
        compose_files=("/srv/demo/compose.yaml",),
        env_file=None,
    )
    tcp = await checker._check_once(
        HealthCheckSpec(
            kind=HealthCheckKind.TCP,
            host="127.0.0.1",
            port=5432,
            interval_seconds=1,
        ),
        session=session,  # type: ignore[arg-type]
        docker=docker,  # type: ignore[arg-type]
        project_name="demo",
        project_directory="/srv/demo",
        compose_files=("/srv/demo/compose.yaml",),
        env_file=None,
    )

    assert http and http["status"] == 204
    assert tcp and tcp["port"] == 5432
    assert any(command[0] == "curl" for command in session.commands)
    assert any(command[0] == "nc" for command in session.commands)


async def test_compose_health_accepts_running_services_without_container_healthcheck() -> None:
    class RunningDocker(FakeDocker):
        async def compose_ps(self, **_: object) -> tuple[dict[str, str], ...]:
            return ({"Service": "app", "State": "running", "Health": ""},)

    session = FakeSession()
    checker = HealthChecker()
    docker = RunningDocker(session)

    result = await checker._check_once(
        HealthCheckSpec(kind=HealthCheckKind.COMPOSE, interval_seconds=1),
        session=session,  # type: ignore[arg-type]
        docker=docker,  # type: ignore[arg-type]
        project_name="demo",
        project_directory="/srv/demo",
        compose_files=("/srv/demo/compose.yaml",),
        env_file="/srv/demo/.env",
    )

    assert result == {"kind": "compose", "services": 1}


async def test_stale_registry_config_cleanup_is_limited_to_current_ssh_uid() -> None:
    valid = "/tmp/light-devops-docker-config-" + "a" * 32
    other = "/tmp/light-devops-docker-config-not-a-uuid"

    class CleanupSession(FakeSession):
        async def run(self, argv, **_: object) -> SSHCommandResult:
            values = tuple(argv)
            self.commands.append(values)
            if values == ("id", "-u"):
                return SSHCommandResult(values, 0, b"1001\n", b"")
            if values and values[0] == "find":
                return SSHCommandResult(
                    values,
                    0,
                    f"{valid}\n{other}\n".encode(),
                    b"",
                )
            return SSHCommandResult(values, 0, b"", b"")

    session = CleanupSession()
    await cleanup_stale_docker_configs(session)  # type: ignore[arg-type]

    find_command = next(command for command in session.commands if command[0] == "find")
    assert ("-uid", "1001") == find_command[find_command.index("-uid") :][:2]
    assert ("rm", "-rf", "--", valid) in session.commands
    assert ("rm", "-rf", "--", other) not in session.commands
