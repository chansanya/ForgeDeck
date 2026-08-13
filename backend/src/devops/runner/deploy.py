"""通过 SSH 与 Docker Compose 执行事务化部署、健康检查、对账和回滚。

远端 revision 与 pending 文件是断线恢复依据，不能用无条件重跑替代状态确认。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import math
import posixpath
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from devops.runner.credentials import RegistryCredentials, docker_config_bytes
from devops.runner.docker import RemoteDockerClient
from devops.runner.ssh import SSHSession

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_COMPOSE_VERSION = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_MINIMUM_COMPOSE_VERSION = (2, 20, 0)
_DOCKER_CONFIG_PREFIX = "/tmp/light-devops-docker-config-"
_DOCKER_CONFIG_PATH = re.compile(
    rf"^{re.escape(_DOCKER_CONFIG_PREFIX)}[0-9a-f]{{32}}$"
)
_EMPTY_DOCKER_CONFIG = b'{"auths":{}}'
_PENDING_TRANSACTION_VERSION = 1


class HealthCheckKind(StrEnum):
    COMPOSE = "compose"
    HTTP = "http"
    TCP = "tcp"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class HealthCheckSpec:
    kind: HealthCheckKind = HealthCheckKind.COMPOSE
    timeout_seconds: float = 120
    interval_seconds: float = 2
    url: str | None = None
    host: str | None = None
    port: int | None = None
    command: tuple[str, ...] = ()
    expected_http_status_min: int = 200
    expected_http_status_max: int = 399

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.interval_seconds <= 0:
            raise ValueError("health-check timing must be positive")
        if self.kind is HealthCheckKind.HTTP and not self.url:
            raise ValueError("HTTP health checks require a URL")
        if self.kind is HealthCheckKind.TCP:
            if not self.host or self.port is None or not 1 <= self.port <= 65535:
                raise ValueError("TCP health checks require a valid host and port")
        if self.kind is HealthCheckKind.COMMAND and not self.command:
            raise ValueError("command health checks require an argument array")


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    project_name: str
    service_name: str
    remote_directory: str
    compose_content: bytes
    image_ref: str
    image_digest: str
    revision: str
    env_content: bytes | None = None
    registry_credentials: RegistryCredentials | None = None
    rollback_registry_credentials: RegistryCredentials | None = None
    rollback_image_ref: str | None = None
    min_free_bytes: int = 512 * 1024 * 1024
    health_check: HealthCheckSpec = HealthCheckSpec()
    rollback_on_failure: bool = True

    def __post_init__(self) -> None:
        if (
            "\x00" in self.remote_directory
            or not self.remote_directory.startswith("/")
            or self.remote_directory == "/"
            or posixpath.normpath(self.remote_directory) != self.remote_directory
        ):
            raise ValueError("remote_directory must be a canonical absolute non-root path")
        if not _SERVICE_NAME.fullmatch(self.service_name):
            raise ValueError("invalid Compose service name")
        if not _DIGEST.fullmatch(self.image_digest):
            raise ValueError("image_digest must be a sha256 digest")
        if not self.image_ref or any(char in self.image_ref for char in "\x00\r\n"):
            raise ValueError("image_ref is invalid")
        if (self.rollback_registry_credentials is None) != (self.rollback_image_ref is None):
            raise ValueError(
                "rollback_registry_credentials and rollback_image_ref must be provided together"
            )
        if self.rollback_image_ref is not None and (
            not self.rollback_image_ref
            or any(char in self.rollback_image_ref for char in "\x00\r\n")
        ):
            raise ValueError("rollback_image_ref is invalid")
        if not self.revision or any(char in self.revision for char in "\x00\r\n"):
            raise ValueError("revision is invalid")
        if not self.compose_content:
            raise ValueError("compose_content cannot be empty")
        if self.min_free_bytes < 0:
            raise ValueError("min_free_bytes cannot be negative")

    @property
    def immutable_image_ref(self) -> str:
        """将可变 tag 替换为 digest，确保 Compose 只拉取构建时确认的镜像。"""
        repository = self.image_ref.split("@", 1)[0]
        slash = repository.rfind("/")
        colon = repository.rfind(":")
        if colon > slash:
            repository = repository[:colon]
        return f"{repository}@{self.image_digest}"


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    revision: str
    previous_revision: str | None
    rolled_back: bool
    health: Mapping[str, object]


class DeploymentError(RuntimeError):
    def __init__(self, message: str, *, rolled_back: bool = False) -> None:
        self.rolled_back = rolled_back
        super().__init__(message)


class DeploymentRollbackError(DeploymentError):
    def __init__(self, deploy_error: Exception, rollback_error: Exception) -> None:
        self.deploy_error = deploy_error
        self.rollback_error = rollback_error
        super().__init__(
            f"deployment failed ({deploy_error}); rollback also failed ({rollback_error})",
            rolled_back=False,
        )


@dataclass(slots=True)
class _RemoteSnapshot:
    compose: bytes | None
    override: bytes | None
    env: bytes | None
    revision: bytes | None


@dataclass(frozen=True, slots=True)
class _PendingTransaction:
    target_revision: str
    target_image: str
    snapshot: _RemoteSnapshot


class HealthChecker:
    async def wait_until_healthy(
        self,
        spec: HealthCheckSpec,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        project_name: str,
        project_directory: str,
        compose_files: tuple[str, ...],
        env_file: str | None,
    ) -> Mapping[str, object]:
        """在超时窗口内轮询 Compose、HTTP、TCP 或受控命令健康状态。"""
        deadline = time.monotonic() + spec.timeout_seconds
        last_error = "health check did not run"
        while True:
            try:
                result = await self._check_once(
                    spec,
                    session=session,
                    docker=docker,
                    project_name=project_name,
                    project_directory=project_directory,
                    compose_files=compose_files,
                    env_file=env_file,
                )
                if result is not None:
                    return result
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"health check timed out: {last_error}")
            await asyncio.sleep(min(spec.interval_seconds, remaining))

    async def _check_once(
        self,
        spec: HealthCheckSpec,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        project_name: str,
        project_directory: str,
        compose_files: tuple[str, ...],
        env_file: str | None,
    ) -> Mapping[str, object] | None:
        if spec.kind is HealthCheckKind.COMPOSE:
            services = await docker.compose_ps(
                project_name=project_name,
                project_directory=project_directory,
                files=compose_files,
                env_file=env_file,
            )
            if not services:
                return None
            for service in services:
                state = str(service.get("State") or service.get("state") or "").lower()
                health = str(service.get("Health") or service.get("health") or "").lower()
                if state not in {"running", "healthy"}:
                    return None
                if health and health != "healthy":
                    return None
            return {"kind": spec.kind.value, "services": len(services)}
        if spec.kind is HealthCheckKind.COMMAND:
            result = await session.run(
                spec.command, timeout_seconds=spec.interval_seconds, check=False
            )
            if result.ok:
                return {"kind": spec.kind.value, "exit_status": result.exit_status}
            return None
        if spec.kind is HealthCheckKind.TCP:
            assert spec.host is not None and spec.port is not None
            result = await session.run(
                (
                    "nc",
                    "-z",
                    "-w",
                    str(max(1, math.ceil(spec.interval_seconds))),
                    spec.host,
                    str(spec.port),
                ),
                timeout_seconds=spec.interval_seconds + 1,
                check=False,
            )
            if result.ok:
                return {"kind": spec.kind.value, "host": spec.host, "port": spec.port}
            return None
        assert spec.url is not None
        status = await _remote_http_status(
            session, spec.url, timeout_seconds=spec.interval_seconds
        )
        if spec.expected_http_status_min <= status <= spec.expected_http_status_max:
            return {"kind": spec.kind.value, "url": spec.url, "status": status}
        return None


class HealthCheckerProtocol(Protocol):
    async def wait_until_healthy(
        self,
        spec: HealthCheckSpec,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        project_name: str,
        project_directory: str,
        compose_files: tuple[str, ...],
        env_file: str | None,
    ) -> Mapping[str, object]: ...


class ComposeDeployer:
    def __init__(self, *, health_checker: HealthCheckerProtocol | None = None) -> None:
        """注入健康检查器，便于测试并保持部署事务流程可替换。"""
        self._health_checker = health_checker or HealthChecker()

    async def deploy(self, request: DeploymentRequest, *, session: SSHSession) -> DeploymentResult:
        """执行预检、原子上传、Compose 部署和失败回滚，并清理临时 Registry 配置。"""
        await cleanup_stale_docker_configs(session)
        docker_config_directory = f"{_DOCKER_CONFIG_PREFIX}{uuid.uuid4().hex}"
        docker_config_path = posixpath.join(docker_config_directory, "config.json")
        try:
            await session.run(
                ("mkdir", "-m", "0700", "--", docker_config_directory),
                check=True,
            )
            config_content = _docker_config_for_request(request)
            await session.write_file_atomic(
                docker_config_path,
                config_content,
                mode=0o600,
            )
            docker = RemoteDockerClient(
                session, docker_config=docker_config_directory
            )
            try:
                return await self._deploy_with_client(request, session=session, docker=docker)
            except asyncio.CancelledError as exc:
                raise DeploymentError("deployment cancelled", rolled_back=False) from exc
        finally:
            with contextlib.suppress(Exception):
                await session.remove_file(docker_config_path)
            with contextlib.suppress(Exception):
                await session.run(
                    ("rmdir", "--", docker_config_directory), check=False
                )

    async def _deploy_with_client(
        self,
        request: DeploymentRequest,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
    ) -> DeploymentResult:
        directory = posixpath.normpath(request.remote_directory)
        try:
            await self._preflight(
                request,
                session=session,
                docker=docker,
                directory=directory,
            )
        except asyncio.CancelledError as exc:
            raise DeploymentError("deployment cancelled", rolled_back=False) from exc
        except Exception as exc:
            raise DeploymentError(str(exc), rolled_back=False) from exc
        compose_path = posixpath.join(directory, "compose.yaml")
        override_path = posixpath.join(directory, "compose.devops.json")
        env_path = posixpath.join(directory, ".env")
        revision_path = posixpath.join(directory, ".devops", "revision.json")
        pending_path = posixpath.join(directory, ".devops", "pending.json")
        snapshot = await self._snapshot(
            session,
            compose_path=compose_path,
            override_path=override_path,
            env_path=env_path,
            revision_path=revision_path,
        )
        override = _compose_override(request)
        compose_files = (compose_path, override_path)
        env_file = env_path

        pending = await _read_pending_transaction(session, pending_path)
        if pending is not None:
            if (
                pending.target_revision == request.revision
                and pending.target_image == request.immutable_image_ref
            ):
                try:
                    reconciled = await self._reconcile(
                        request,
                        session=session,
                        docker=docker,
                        snapshot=snapshot,
                        directory=directory,
                        compose_files=compose_files,
                        env_path=env_path,
                        revision_path=revision_path,
                    )
                except DeploymentError:
                    reconciled = None
                if reconciled is not None:
                    await _clear_pending_transaction(session, pending_path)
                    return reconciled
            await self._recover_pending_transaction(
                request,
                pending,
                current_snapshot=snapshot,
                session=session,
                docker=docker,
                compose_path=compose_path,
                override_path=override_path,
                env_path=env_path,
                revision_path=revision_path,
                pending_path=pending_path,
                directory=directory,
                compose_files=compose_files,
            )
            snapshot = await self._snapshot(
                session,
                compose_path=compose_path,
                override_path=override_path,
                env_path=env_path,
                revision_path=revision_path,
            )

        previous_revision = _parse_revision(snapshot.revision)
        reconciled = await self._reconcile(
            request,
            session=session,
            docker=docker,
            snapshot=snapshot,
            directory=directory,
            compose_files=compose_files,
            env_path=env_path,
            revision_path=revision_path,
        )
        if reconciled is not None:
            return reconciled

        try:
            # pending 文件必须先于 Compose 变更落盘，进程或 SSH 中断后才能判断
            # 远端处于未完成事务，而不是盲目重跑可能非幂等的部署步骤。
            await _write_pending_transaction(
                session,
                pending_path,
                request=request,
                snapshot=snapshot,
            )
        except asyncio.CancelledError as exc:
            raise DeploymentError("deployment cancelled", rolled_back=False) from exc
        except Exception as exc:
            raise DeploymentError(
                f"failed to persist deployment transaction: {exc}",
                rolled_back=False,
            ) from exc

        try:
            await _write_control_file(
                session, compose_path, request.compose_content, mode=0o600
            )
            await _write_control_file(session, override_path, override, mode=0o600)
            await _write_control_file(
                session, env_path, request.env_content or b"", mode=0o600
            )
            await docker.compose_up(
                project_name=request.project_name,
                project_directory=directory,
                files=compose_files,
                env_file=env_file,
                wait=True,
                wait_timeout_seconds=max(1, int(request.health_check.timeout_seconds)),
            )
            health = await self._health_checker.wait_until_healthy(
                request.health_check,
                session=session,
                docker=docker,
                project_name=request.project_name,
                project_directory=directory,
                compose_files=compose_files,
                env_file=env_file,
            )
            revision_record = json.dumps(
                {
                    "revision": request.revision,
                    "previous_revision": previous_revision,
                    "image": request.immutable_image_ref,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            await _write_control_file(session, revision_path, revision_record, mode=0o600)
        except asyncio.CancelledError as deploy_error:
            rolled_back = await self._rollback_failed_transaction(
                request,
                snapshot,
                deploy_error,
                session=session,
                docker=docker,
                compose_path=compose_path,
                override_path=override_path,
                env_path=env_path,
                revision_path=revision_path,
                pending_path=pending_path,
                directory=directory,
                compose_files=compose_files,
            )
            raise DeploymentError(
                "deployment cancelled",
                rolled_back=rolled_back,
            ) from deploy_error
        except Exception as deploy_error:
            if not request.rollback_on_failure:
                await _clear_pending_transaction(session, pending_path)
                raise DeploymentError(str(deploy_error), rolled_back=False) from deploy_error
            rolled_back = await self._rollback_failed_transaction(
                request,
                snapshot,
                deploy_error,
                session=session,
                docker=docker,
                compose_path=compose_path,
                override_path=override_path,
                env_path=env_path,
                revision_path=revision_path,
                pending_path=pending_path,
                directory=directory,
                compose_files=compose_files,
            )
            raise DeploymentError(str(deploy_error), rolled_back=rolled_back) from deploy_error
        await _clear_pending_transaction(session, pending_path)
        return DeploymentResult(
            revision=request.revision,
            previous_revision=previous_revision,
            rolled_back=False,
            health=health,
        )

    async def _recover_pending_transaction(
        self,
        request: DeploymentRequest,
        pending: _PendingTransaction,
        *,
        current_snapshot: _RemoteSnapshot,
        session: SSHSession,
        docker: RemoteDockerClient,
        compose_path: str,
        override_path: str,
        env_path: str,
        revision_path: str,
        pending_path: str,
        directory: str,
        compose_files: tuple[str, ...],
    ) -> None:
        try:
            if _is_successful_snapshot(pending.snapshot):
                await self._restore(
                    session,
                    pending.snapshot,
                    compose_path=compose_path,
                    override_path=override_path,
                    env_path=env_path,
                    revision_path=revision_path,
                )
                await docker.compose_up(
                    project_name=request.project_name,
                    project_directory=directory,
                    files=compose_files,
                    env_file=env_path if pending.snapshot.env is not None else None,
                    wait=True,
                    wait_timeout_seconds=max(1, int(request.health_check.timeout_seconds)),
                )
                await self._wait_for_rollback_health(
                    request,
                    session=session,
                    docker=docker,
                    directory=directory,
                    compose_files=compose_files,
                    env_file=env_path if pending.snapshot.env is not None else None,
                )
            else:
                if (
                    current_snapshot.compose is not None
                    and current_snapshot.override is not None
                ):
                    await docker.compose_down(
                        project_name=request.project_name,
                        project_directory=directory,
                        files=compose_files,
                        env_file=env_path if current_snapshot.env is not None else None,
                    )
                await self._restore(
                    session,
                    pending.snapshot,
                    compose_path=compose_path,
                    override_path=override_path,
                    env_path=env_path,
                    revision_path=revision_path,
                )
            await _clear_pending_transaction(session, pending_path)
        except Exception as recovery_error:
            raise DeploymentError(
                "an unfinished deployment transaction could not be recovered: "
                f"{recovery_error}",
                rolled_back=False,
            ) from recovery_error

    async def _rollback_failed_transaction(
        self,
        request: DeploymentRequest,
        snapshot: _RemoteSnapshot,
        deploy_error: BaseException,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        compose_path: str,
        override_path: str,
        env_path: str,
        revision_path: str,
        pending_path: str,
        directory: str,
        compose_files: tuple[str, ...],
    ) -> bool:
        # 只有存在完整成功快照时才重新拉起上一版本；首次部署失败则应停止
        # 新建的 Compose 项目并恢复控制文件，不能伪装成“已回滚”。
        rolled_back = _is_successful_snapshot(snapshot)
        try:
            if rolled_back:
                await self._restore(
                    session,
                    snapshot,
                    compose_path=compose_path,
                    override_path=override_path,
                    env_path=env_path,
                    revision_path=revision_path,
                )
                await docker.compose_up(
                    project_name=request.project_name,
                    project_directory=directory,
                    files=compose_files,
                    env_file=env_path if snapshot.env is not None else None,
                    wait=True,
                    wait_timeout_seconds=max(1, int(request.health_check.timeout_seconds)),
                )
                await self._wait_for_rollback_health(
                    request,
                    session=session,
                    docker=docker,
                    directory=directory,
                    compose_files=compose_files,
                    env_file=env_path if snapshot.env is not None else None,
                )
            else:
                await docker.compose_down(
                    project_name=request.project_name,
                    project_directory=directory,
                    files=compose_files,
                    env_file=env_path,
                )
                await self._restore(
                    session,
                    snapshot,
                    compose_path=compose_path,
                    override_path=override_path,
                    env_path=env_path,
                    revision_path=revision_path,
                )
            await _clear_pending_transaction(session, pending_path)
        except Exception as rollback_error:
            source_error = (
                deploy_error
                if isinstance(deploy_error, Exception)
                else RuntimeError(type(deploy_error).__name__)
            )
            raise DeploymentRollbackError(source_error, rollback_error) from rollback_error
        return rolled_back

    async def _wait_for_rollback_health(
        self,
        request: DeploymentRequest,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        directory: str,
        compose_files: tuple[str, ...],
        env_file: str | None,
    ) -> None:
        await self._health_checker.wait_until_healthy(
            HealthCheckSpec(
                kind=HealthCheckKind.COMPOSE,
                timeout_seconds=request.health_check.timeout_seconds,
                interval_seconds=request.health_check.interval_seconds,
            ),
            session=session,
            docker=docker,
            project_name=request.project_name,
            project_directory=directory,
            compose_files=compose_files,
            env_file=env_file,
        )

    async def _reconcile(
        self,
        request: DeploymentRequest,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        snapshot: _RemoteSnapshot,
        directory: str,
        compose_files: tuple[str, ...],
        env_path: str,
        revision_path: str,
    ) -> DeploymentResult | None:
        if snapshot.compose is None or snapshot.override is None:
            return None
        try:
            services = await docker.compose_ps(
                project_name=request.project_name,
                project_directory=directory,
                files=compose_files,
                env_file=env_path if snapshot.env is not None else None,
            )
            service = next(
                (
                    item
                    for item in services
                    if str(item.get("Service") or item.get("service") or "")
                    == request.service_name
                ),
                None,
            )
            if service is None:
                return None
            container_name = service.get("Name") or service.get("name")
            if not isinstance(container_name, str) or not container_name:
                return None
            inspected = await docker.inspect_container(container_name)
        except Exception:
            return None
        config = inspected.get("Config")
        state = inspected.get("State")
        if not isinstance(config, Mapping) or not isinstance(state, Mapping):
            return None
        labels = config.get("Labels") or {}
        if not isinstance(labels, Mapping):
            return None
        health_value = state.get("Health") or {}
        health_status = (
            str(health_value.get("Status") or "").lower()
            if isinstance(health_value, Mapping)
            else ""
        )
        state_status = str(state.get("Status") or "").lower()
        if (
            labels.get("devops.revision") != request.revision
            or config.get("Image") != request.immutable_image_ref
            or state_status != "running"
            or (health_status and health_status != "healthy")
        ):
            return None
        try:
            compose_health = await self._health_checker.wait_until_healthy(
                HealthCheckSpec(
                    kind=HealthCheckKind.COMPOSE,
                    timeout_seconds=request.health_check.timeout_seconds,
                    interval_seconds=request.health_check.interval_seconds,
                ),
                session=session,
                docker=docker,
                project_name=request.project_name,
                project_directory=directory,
                compose_files=compose_files,
                env_file=env_path if snapshot.env is not None else None,
            )
            configured_health = (
                compose_health
                if request.health_check.kind is HealthCheckKind.COMPOSE
                else await self._health_checker.wait_until_healthy(
                    request.health_check,
                    session=session,
                    docker=docker,
                    project_name=request.project_name,
                    project_directory=directory,
                    compose_files=compose_files,
                    env_file=env_path if snapshot.env is not None else None,
                )
            )
        except Exception as exc:
            raise DeploymentError(
                f"reconciled deployment failed health checks: {exc}",
                rolled_back=False,
            ) from exc
        recorded_revision, recorded_previous = _parse_revision_record(snapshot.revision)
        previous_revision = (
            recorded_previous if recorded_revision == request.revision else recorded_revision
        )
        revision_record = json.dumps(
            {
                "revision": request.revision,
                "previous_revision": previous_revision,
                "image": request.immutable_image_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        await _write_control_file(session, revision_path, revision_record, mode=0o600)
        return DeploymentResult(
            revision=request.revision,
            previous_revision=previous_revision,
            rolled_back=False,
            health={
                "kind": "reconciled",
                "compose": dict(compose_health),
                "configured": dict(configured_health),
            },
        )

    async def _preflight(
        self,
        request: DeploymentRequest,
        *,
        session: SSHSession,
        docker: RemoteDockerClient,
        directory: str,
    ) -> None:
        await docker.version()
        compose_version = await docker.compose_version()
        _require_minimum_compose_version(compose_version)
        await _verify_health_check_tool(request.health_check, session=session)
        devops_directory = posixpath.join(directory, ".devops")
        await session.run(
            ("mkdir", "-p", "--", directory, devops_directory), check=True
        )
        resolved = await session.run(("realpath", "-m", "--", directory), check=True)
        # canonical deploy_path 和无 symlink 约束共同防止管理员配置被远端链接劫持，
        # 否则上传 Compose/环境文件可能覆盖目标目录之外的任意文件。
        if resolved.stdout.decode(errors="replace").strip() != directory:
            raise RuntimeError("deployment preflight failed: deploy_path resolves elsewhere")
        resolved_devops = await session.run(
            ("realpath", "-m", "--", devops_directory), check=True
        )
        if resolved_devops.stdout.decode(errors="replace").strip() != devops_directory:
            raise RuntimeError(
                "deployment preflight failed: .devops directory resolves elsewhere"
            )
        await _assert_no_symlinks(
            session,
            (
                directory,
                posixpath.join(directory, "compose.yaml"),
                posixpath.join(directory, "compose.devops.json"),
                posixpath.join(directory, ".env"),
                devops_directory,
                posixpath.join(devops_directory, "revision.json"),
                posixpath.join(devops_directory, "pending.json"),
            ),
        )
        result = await session.run(("df", "-Pk", directory), check=True)
        available = _parse_available_bytes(result.stdout)
        if available < request.min_free_bytes:
            raise RuntimeError(
                "deployment preflight failed: insufficient disk space "
                f"({available} bytes available, {request.min_free_bytes} required)"
            )

    async def _snapshot(
        self,
        session: SSHSession,
        *,
        compose_path: str,
        override_path: str,
        env_path: str,
        revision_path: str,
    ) -> _RemoteSnapshot:
        return _RemoteSnapshot(
            compose=await _read_if_exists(session, compose_path),
            override=await _read_if_exists(session, override_path),
            env=await _read_if_exists(session, env_path),
            revision=await _read_if_exists(session, revision_path),
        )

    async def _restore(
        self,
        session: SSHSession,
        snapshot: _RemoteSnapshot,
        *,
        compose_path: str,
        override_path: str,
        env_path: str,
        revision_path: str,
    ) -> None:
        for path, value in (
            (compose_path, snapshot.compose),
            (override_path, snapshot.override),
            (env_path, snapshot.env),
            (revision_path, snapshot.revision),
        ):
            if value is None:
                await _assert_safe_parent(session, path)
                await session.remove_file(path)
            else:
                await _write_control_file(session, path, value, mode=0o600)


async def _write_pending_transaction(
    session: SSHSession,
    path: str,
    *,
    request: DeploymentRequest,
    snapshot: _RemoteSnapshot,
) -> None:
    payload = {
        "version": _PENDING_TRANSACTION_VERSION,
        "target_revision": request.revision,
        "target_image": request.immutable_image_ref,
        "snapshot": {
            "compose": _encode_snapshot_value(snapshot.compose),
            "override": _encode_snapshot_value(snapshot.override),
            "env": _encode_snapshot_value(snapshot.env),
            "revision": _encode_snapshot_value(snapshot.revision),
        },
    }
    await _write_control_file(
        session,
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        mode=0o600,
    )


async def _read_pending_transaction(
    session: SSHSession,
    path: str,
) -> _PendingTransaction | None:
    raw = await _read_if_exists(session, path)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("pending deployment transaction is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != _PENDING_TRANSACTION_VERSION:
        raise RuntimeError("pending deployment transaction has an unsupported version")
    target_revision = payload.get("target_revision")
    target_image = payload.get("target_image")
    snapshot_payload = payload.get("snapshot")
    if (
        not isinstance(target_revision, str)
        or not target_revision
        or not isinstance(target_image, str)
        or not target_image
        or not isinstance(snapshot_payload, dict)
    ):
        raise RuntimeError("pending deployment transaction is incomplete")
    try:
        snapshot = _RemoteSnapshot(
            compose=_decode_snapshot_value(snapshot_payload.get("compose")),
            override=_decode_snapshot_value(snapshot_payload.get("override")),
            env=_decode_snapshot_value(snapshot_payload.get("env")),
            revision=_decode_snapshot_value(snapshot_payload.get("revision")),
        )
    except ValueError as exc:
        raise RuntimeError(f"pending deployment transaction is corrupt: {exc}") from exc
    return _PendingTransaction(
        target_revision=target_revision,
        target_image=target_image,
        snapshot=snapshot,
    )


async def _clear_pending_transaction(session: SSHSession, path: str) -> None:
    await _assert_safe_parent(session, path)
    await _assert_no_symlinks(session, (path,))
    await session.remove_file(path)


def _encode_snapshot_value(value: bytes | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "data": base64.b64encode(value).decode("ascii"),
        "sha256": hashlib.sha256(value).hexdigest(),
    }


def _decode_snapshot_value(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("snapshot entry must be an object or null")
    encoded = value.get("data")
    expected_hash = value.get("sha256")
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        raise ValueError("snapshot entry is missing data or sha256")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("snapshot entry contains invalid base64") from exc
    actual_hash = hashlib.sha256(decoded).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ValueError("snapshot entry sha256 does not match")
    return decoded


def _is_successful_snapshot(snapshot: _RemoteSnapshot) -> bool:
    return (
        snapshot.compose is not None
        and snapshot.override is not None
        and _parse_revision(snapshot.revision) is not None
    )


async def _read_if_exists(session: SSHSession, path: str) -> bytes | None:
    await _assert_safe_parent(session, path)
    await _assert_no_symlinks(session, (path,))
    if not await session.exists(path):
        return None
    return await session.read_file(path)


async def _write_control_file(
    session: SSHSession, path: str, data: bytes, *, mode: int
) -> None:
    await _assert_safe_parent(session, path)
    await _assert_no_symlinks(session, (path,))
    await session.write_file_atomic(path, data, mode=mode)


async def _assert_safe_parent(session: SSHSession, path: str) -> None:
    parent = posixpath.dirname(path)
    resolved = await session.run(("realpath", "-m", "--", parent), check=True)
    if resolved.stdout.decode(errors="replace").strip() != parent:
        raise RuntimeError(f"refusing control-file access through a symlinked directory: {parent}")


async def _assert_no_symlinks(session: SSHSession, paths: tuple[str, ...]) -> None:
    for path in paths:
        result = await session.run(
            ("find", path, "-maxdepth", "0", "-type", "l", "-print"),
            check=False,
        )
        if result.stdout.strip():
            raise RuntimeError(f"refusing symlinked deployment control path: {path}")


async def cleanup_stale_docker_configs(session: SSHSession) -> None:
    uid_result = await session.run(("id", "-u"), check=False)
    uid = uid_result.stdout.decode(errors="replace").strip()
    if not uid_result.ok or not uid.isascii() or not uid.isdecimal():
        return
    result = await session.run(
        (
            "find",
            "/tmp",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-uid",
            uid,
            "-name",
            "light-devops-docker-config-*",
            "-mmin",
            "+60",
            "-print",
        ),
        check=False,
    )
    if not result.ok:
        return
    for raw_path in result.stdout.decode(errors="replace").splitlines():
        path = raw_path.strip()
        if not _DOCKER_CONFIG_PATH.fullmatch(path):
            continue
        with contextlib.suppress(Exception):
            await session.run(("rm", "-rf", "--", path), check=False)


def _require_minimum_compose_version(value: str) -> None:
    match = _COMPOSE_VERSION.fullmatch(value.strip())
    if match is None:
        raise RuntimeError(
            f"deployment preflight failed: unsupported Docker Compose version {value!r}"
        )
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    if version < _MINIMUM_COMPOSE_VERSION:
        raise RuntimeError(
            "deployment preflight failed: Docker Compose 2.20.0 or newer is required "
            f"(found {value})"
        )


async def _verify_health_check_tool(
    spec: HealthCheckSpec, *, session: SSHSession
) -> None:
    if spec.kind is HealthCheckKind.HTTP:
        executable = "curl"
        probe = (executable, "--version")
        accepted_exit_statuses = {0}
    elif spec.kind is HealthCheckKind.TCP:
        executable = "nc"
        probe = (executable, "-h")
        accepted_exit_statuses = {0, 1}
    else:
        return
    result = await session.run(probe, timeout_seconds=5, check=False)
    if result.exit_status not in accepted_exit_statuses:
        raise RuntimeError(
            f"deployment preflight failed: {executable} is required for "
            f"{spec.kind.value} health checks"
        )


def _parse_available_bytes(value: bytes) -> int:
    lines = [line.split() for line in value.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[-1]) < 4:
        raise ValueError("df returned an unexpected response")
    try:
        blocks = int(lines[-1][3])
    except ValueError as exc:
        raise ValueError("df returned an invalid available-block count") from exc
    if blocks < 0:
        raise ValueError("df returned a negative available-block count")
    return blocks * 1024


def _parse_revision(value: bytes | None) -> str | None:
    revision, _ = _parse_revision_record(value)
    return revision


def _parse_revision_record(value: bytes | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    revision = parsed.get("revision")
    previous = parsed.get("previous_revision")
    return (
        revision if isinstance(revision, str) else None,
        previous if isinstance(previous, str) else None,
    )


def _compose_override(request: DeploymentRequest) -> bytes:
    return json.dumps(
        {
            "services": {
                request.service_name: {
                    "image": request.immutable_image_ref,
                    "labels": {"devops.revision": request.revision},
                }
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _docker_config_for_request(request: DeploymentRequest) -> bytes:
    credentials_by_image: list[tuple[RegistryCredentials, str]] = []
    if request.registry_credentials is not None:
        credentials_by_image.append((request.registry_credentials, request.image_ref))
    if (
        request.rollback_registry_credentials is not None
        and request.rollback_image_ref is not None
    ):
        credentials_by_image.append(
            (request.rollback_registry_credentials, request.rollback_image_ref)
        )
    if not credentials_by_image:
        return _EMPTY_DOCKER_CONFIG
    auths: dict[str, object] = {}
    for credentials, image_ref in credentials_by_image:
        parsed = json.loads(docker_config_bytes(credentials, image_ref))
        for endpoint, value in parsed.get("auths", {}).items():
            auths.setdefault(endpoint, value)
    return json.dumps({"auths": auths}, sort_keys=True, separators=(",", ":")).encode()


async def _remote_http_status(
    session: SSHSession, url: str, *, timeout_seconds: float
) -> int:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("health-check URL must use http or https")
    timeout = max(1, math.ceil(timeout_seconds))
    result = await session.run(
        (
            "curl",
            "--silent",
            "--show-error",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--connect-timeout",
            str(timeout),
            "--max-time",
            str(timeout),
            "--",
            url,
        ),
        timeout_seconds=timeout_seconds + 1,
        check=False,
    )
    result.check_returncode()
    try:
        return int(result.stdout.decode().strip())
    except ValueError as exc:
        raise ValueError("health endpoint returned an invalid HTTP status") from exc
