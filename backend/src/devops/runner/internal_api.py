"""暴露仅供 API 调用的 Runner 类型化 HTTP 与 SSH WebSocket 接口。"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import posixpath
from typing import Annotated, Literal

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from devops.domain.models import AuditEvent, DeploymentEnvironment, Project
from devops.runner.docker import RemoteDockerClient
from devops.runner.handlers import (
    RunnerDependencies,
    _compose_project_name,
    _load_ssh_target,
)
from devops.runner.ssh import SSHSession, SSHTerminal
from devops.schemas import HostKeyScanRead, HostKeyScanRequest

logger = logging.getLogger(__name__)


class DockerActionBody(BaseModel):
    name: str | None = Field(default=None, max_length=1000)
    confirmation: str | None = Field(default=None, max_length=1000)
    environment_id: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=300)


DockerAction = Literal[
    "container-start",
    "container-stop",
    "container-restart",
    "container-remove",
    "volume-remove",
    "image-remove",
    "network-remove",
    "compose-up",
    "compose-down",
    "compose-restart",
]


def create_internal_app(*, dependencies: RunnerDependencies, token: str) -> FastAPI:
    """创建仅供 API 进程调用的本机 Runner 服务，不对公网暴露 Docker 权限。"""
    if len(token) < 32:
        raise ValueError("DEVOPS_INTERNAL_TOKEN must contain at least 32 characters")
    app = FastAPI(
        title="Light DevOps Runner Internal API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        """校验内部 Bearer Token，阻断未授权的 Docker/SSH 代理请求。"""
        _verify_bearer(authorization, token)

    @app.get("/internal/health", dependencies=[Depends(authorize)])
    async def health() -> dict[str, str]:
        """返回 Runner 存活状态，不触发外部副作用。"""
        return {"status": "ok"}

    @app.post(
        "/internal/ssh/host-key/scan",
        response_model=HostKeyScanRead,
        dependencies=[Depends(authorize)],
    )
    async def scan_host_key(body: HostKeyScanRequest) -> HostKeyScanRead:
        """扫描目标主机公钥，供管理员确认后再登记指纹。"""
        try:
            scanned = await dependencies.ssh.scan_host_key(body.host, body.port)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.warning("SSH host-key scan failed for %s:%s: %s", body.host, body.port, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="SSH host-key scan failed",
            ) from exc
        return HostKeyScanRead(
            algorithm=scanned.algorithm,
            fingerprint=scanned.fingerprint,
            public_key=scanned.public_key,
        )

    @app.get(
        "/internal/servers/{server_id}/docker/overview",
        dependencies=[Depends(authorize)],
    )
    async def docker_overview(server_id: str) -> dict[str, object]:
        """通过 Runner 读取目标机 Docker 资源概览。"""
        try:
            config, credentials, _ = await _load_ssh_target(dependencies, server_id)
            async with dependencies.ssh.connect(config, credentials) as session:
                client = RemoteDockerClient(session)
                version, disk_usage, containers, images, volumes, networks = await asyncio.gather(
                    client.version(),
                    client.disk_usage(),
                    client.list_containers(),
                    client.list_images(),
                    client.list_volumes(),
                    client.list_networks(),
                )
        except Exception as exc:
            logger.warning("remote Docker overview failed for %s: %s", server_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Remote Docker query failed",
            ) from exc
        return {
            "server_id": server_id,
            "version": version,
            "disk_usage": disk_usage,
            "containers": containers,
            "images": images,
            "volumes": volumes,
            "networks": networks,
        }

    @app.post(
        "/internal/servers/{server_id}/docker/actions/{action}",
        dependencies=[Depends(authorize)],
    )
    async def docker_action(
        server_id: str,
        action: DockerAction,
        body: DockerActionBody,
        x_devops_actor: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """执行白名单 Docker 操作，并返回可审计的结构化结果。"""
        try:
            config, credentials, _ = await _load_ssh_target(dependencies, server_id)
            async with dependencies.ssh.connect(config, credentials) as session:
                client = RemoteDockerClient(session)
                await _execute_docker_action(
                    dependencies,
                    client,
                    session,
                    server_id=server_id,
                    action=action,
                    body=body,
                )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except Exception as exc:
            logger.warning("remote Docker action failed for %s: %s", server_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Remote Docker action failed",
            ) from exc
        try:
            await _audit(
                dependencies,
                actor=x_devops_actor or "internal-api",
                action=f"docker.{action}",
                resource_type="server",
                resource_id=server_id,
                details={"name": body.name, "environment_id": body.environment_id},
            )
        except Exception:
            logger.exception("failed to persist Docker action audit event")
        return {"ok": True, "action": action, "server_id": server_id}

    @app.websocket("/internal/servers/{server_id}/terminal")
    async def terminal(websocket: WebSocket, server_id: str) -> None:
        """转发已登记服务器的 SSH PTY，终端内容不写入持久录像。"""
        try:
            _verify_bearer(websocket.headers.get("authorization"), token)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        actor = websocket.headers.get("x-devops-actor") or "internal-api"
        await _audit(
            dependencies,
            actor=actor,
            action="ssh.terminal.open",
            resource_type="server",
            resource_id=server_id,
        )
        try:
            config, credentials, _ = await _load_ssh_target(dependencies, server_id)
            async with dependencies.ssh.connect(config, credentials) as session:
                terminal_session = await session.open_terminal()
                outbound = asyncio.create_task(_terminal_to_websocket(terminal_session, websocket))
                inbound = asyncio.create_task(_websocket_to_terminal(websocket, terminal_session))
                done, pending = await asyncio.wait(
                    {outbound, inbound}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done | pending:
                    with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                        await task
                with contextlib.suppress(Exception):
                    await terminal_session.close()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("SSH terminal session failed for server %s", server_id)
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1011)
        finally:
            try:
                await _audit(
                    dependencies,
                    actor=actor,
                    action="ssh.terminal.close",
                    resource_type="server",
                    resource_id=server_id,
                )
            except Exception:
                logger.exception("failed to persist SSH terminal close audit event")

    return app


async def _execute_docker_action(
    dependencies: RunnerDependencies,
    client: RemoteDockerClient,
    session: SSHSession,
    *,
    server_id: str,
    action: DockerAction,
    body: DockerActionBody,
) -> None:
    if action in {"container-start", "container-stop", "container-restart"}:
        if not body.name:
            raise ValueError("container action requires name")
        await client.container_action(
            body.name,
            action.removeprefix("container-"),
            timeout_seconds=body.timeout_seconds,
        )
        return
    if action == "container-remove":
        if not body.name or body.confirmation is None:
            raise ValueError("container removal requires name and confirmation")
        await client.remove_container(body.name, confirmation=body.confirmation)
        return
    if action == "volume-remove":
        if not body.name or body.confirmation is None:
            raise ValueError("volume removal requires name and confirmation")
        await client.remove_volume(body.name, confirmation=body.confirmation)
        return
    if action == "image-remove":
        if not body.name or body.confirmation is None:
            raise ValueError("image removal requires name and confirmation")
        await client.remove_image(body.name, confirmation=body.confirmation)
        return
    if action == "network-remove":
        if not body.name or body.confirmation is None:
            raise ValueError("network removal requires name and confirmation")
        await client.remove_network(body.name, confirmation=body.confirmation)
        return
    if body.environment_id is None:
        raise ValueError("Compose action requires environment_id")
    async with dependencies.session_factory() as db_session:
        environment = await db_session.get(DeploymentEnvironment, body.environment_id)
        if environment is None or environment.server_id != server_id:
            raise ValueError("environment does not belong to the target server")
        project = await db_session.get(Project, environment.project_id)
        if project is None:
            raise ValueError("environment project no longer exists")
        directory = posixpath.normpath(environment.deploy_path)
        project_name = _compose_project_name(project.id, environment.id)
    compose_path = posixpath.join(directory, "compose.yaml")
    override_path = posixpath.join(directory, "compose.devops.json")
    env_path = posixpath.join(directory, ".env")
    env_file = env_path if await session.exists(env_path) else None
    files = (compose_path, override_path)
    if action == "compose-up":
        await client.compose_up(
            project_name=project_name,
            project_directory=directory,
            files=files,
            env_file=env_file,
            wait=True,
            wait_timeout_seconds=body.timeout_seconds,
        )
    elif action == "compose-down":
        await client.compose_down(
            project_name=project_name,
            project_directory=directory,
            files=files,
            env_file=env_file,
        )
    else:
        await client.compose_restart(
            project_name=project_name,
            project_directory=directory,
            files=files,
            env_file=env_file,
            timeout_seconds=body.timeout_seconds,
        )


async def _terminal_to_websocket(terminal: SSHTerminal, websocket: WebSocket) -> None:
    while data := await terminal.read():
        await websocket.send_bytes(data)


async def _websocket_to_terminal(websocket: WebSocket, terminal: SSHTerminal) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(code=message.get("code", 1000))
        if message.get("bytes") is not None:
            await terminal.write(message["bytes"])
            continue
        raw_text = message.get("text")
        if raw_text is None:
            continue
        try:
            value = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError("terminal text frames must be JSON control messages") from exc
        if value.get("type") == "input" and isinstance(value.get("data"), str):
            await terminal.write(value["data"].encode())
        elif value.get("type") == "resize":
            await terminal.resize(int(value["columns"]), int(value["rows"]))
        else:
            raise ValueError("unsupported terminal control message")


def _verify_bearer(authorization: str | None, expected_token: str) -> None:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    if not hmac.compare_digest(authorization[len(prefix) :], expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def _audit(
    dependencies: RunnerDependencies,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, object] | None = None,
) -> None:
    async with dependencies.session_factory() as session:
        session.add(
            AuditEvent(
                actor=actor[:255],
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
            )
        )
        await session.commit()
