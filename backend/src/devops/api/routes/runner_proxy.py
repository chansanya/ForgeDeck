"""将类型化 Docker 请求和一次性 SSH WebSocket 会话代理到 Runner。

代理层只转发受控能力，不向浏览器暴露 Runner 内部 Token 或任意命令接口。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import Server
from devops.services import add_audit

router = APIRouter(prefix="/servers", tags=["runner-proxy"])
ssh_router = APIRouter(prefix="/ssh", tags=["runner-proxy"])

_DOCKER_ACTIONS = {
    "container_start": "container-start",
    "container-start": "container-start",
    "container_stop": "container-stop",
    "container-stop": "container-stop",
    "container_restart": "container-restart",
    "container-restart": "container-restart",
    "container_remove": "container-remove",
    "container-remove": "container-remove",
    "image_remove": "image-remove",
    "image-remove": "image-remove",
    "volume_remove": "volume-remove",
    "volume-remove": "volume-remove",
    "network_remove": "network-remove",
    "network-remove": "network-remove",
    "compose_up": "compose-up",
    "compose-up": "compose-up",
    "compose_down": "compose-down",
    "compose-down": "compose-down",
    "compose_restart": "compose-restart",
    "compose-restart": "compose-restart",
}


class DockerActionPayload(BaseModel):
    target: str = Field(min_length=1, max_length=500)
    confirmation: str | None = Field(default=None, max_length=500)
    options: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Ticket:
    server_id: str
    username: str
    expires_at: float


class _TerminalTickets:
    def __init__(self) -> None:
        self._tickets: dict[str, _Ticket] = {}
        self._lock = asyncio.Lock()

    async def issue(self, *, server_id: str, username: str, ttl_seconds: int = 60) -> str:
        """签发绑定服务器和用户的短期 Runner 代理票据。"""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            self._tickets[token] = _Ticket(server_id, username, now + ttl_seconds)
        return token

    async def consume(self, token: str, *, server_id: str | None = None) -> _Ticket | None:
        """一次性消费票据，并校验服务器绑定以阻止跨主机重放。"""
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            ticket = self._tickets.pop(token, None)
        if (
            ticket is None
            or (server_id is not None and ticket.server_id != server_id)
            or ticket.expires_at <= now
        ):
            return None
        return ticket

    def _prune(self, now: float) -> None:
        expired = [token for token, ticket in self._tickets.items() if ticket.expires_at <= now]
        for token in expired:
            self._tickets.pop(token, None)


_terminal_tickets = _TerminalTickets()


async def _require_server(session: SessionDep, server_id: str) -> Server:
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return server


@router.get("/{server_id}/docker/overview")
async def docker_overview(
    server_id: str,
    request: Request,
    _: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """通过短期内部授权向 Runner 请求 Docker 概览。"""
    await _require_server(session, server_id)
    return await _runner_json(request, "GET", f"/internal/servers/{server_id}/docker/overview")


@router.post("/{server_id}/docker/actions/{action}")
async def docker_action(
    server_id: str,
    action: str,
    payload: DockerActionPayload,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """代理白名单 Docker 操作，并在 API 侧写入审计记录。"""
    await _require_server(session, server_id)
    internal_action = _DOCKER_ACTIONS.get(action)
    if internal_action is None:
        raise HTTPException(status_code=404, detail="Unknown Docker action")
    internal_payload = {
        "name": payload.target,
        "confirmation": payload.confirmation,
        "environment_id": payload.options.get("environment_id"),
        "timeout_seconds": payload.options.get("timeout_seconds", 30),
    }
    result = await _runner_json(
        request,
        "POST",
        f"/internal/servers/{server_id}/docker/actions/{internal_action}",
        json=internal_payload,
        actor=user.username,
    )
    await add_audit(
        session,
        actor=user.username,
        action=f"docker.{action}",
        resource_type="server",
        resource_id=server_id,
        details={"target": payload.target},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return result


@router.post("/{server_id}/ssh-sessions", status_code=status.HTTP_201_CREATED)
async def create_ssh_session(
    server_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """创建一次性 SSH 会话票据，不在 API 进程保存终端内容。"""
    await _require_server(session, server_id)
    ticket = await _terminal_tickets.issue(server_id=server_id, username=user.username)
    websocket_path = (
        f"{request.app.state.settings.api_prefix}/ssh/sessions/{quote(ticket)}"
    )
    await add_audit(
        session,
        actor=user.username,
        action="ssh.session.request",
        resource_type="server",
        resource_id=server_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return {
        "id": ticket,
        "websocket_url": websocket_path,
        "expires_in": 60,
    }


@router.websocket("/{server_id}/terminal")
async def ssh_terminal(websocket: WebSocket, server_id: str) -> None:
    """兼容旧路由，将服务器终端请求转交统一代理逻辑。"""
    ticket_value = websocket.query_params.get("ticket", "")
    ticket = await _terminal_tickets.consume(ticket_value, server_id=server_id)
    if ticket is None:
        await websocket.close(code=4401, reason="Invalid or expired terminal ticket")
        return

    await _proxy_terminal(websocket, ticket)


@ssh_router.websocket("/sessions/{session_id}")
async def ssh_session_terminal(websocket: WebSocket, session_id: str) -> None:
    """校验会话票据后转发浏览器与 Runner 的 PTY 数据。"""
    ticket = await _terminal_tickets.consume(session_id)
    if ticket is None:
        await websocket.close(code=4401, reason="Invalid or expired terminal ticket")
        return
    await _proxy_terminal(websocket, ticket)


async def _proxy_terminal(websocket: WebSocket, ticket: _Ticket) -> None:
    """在浏览器和 Runner 之间双向转发 WebSocket，并在一端断开时清理另一端。"""
    server_id = ticket.server_id

    settings = websocket.app.state.settings
    if not settings.internal_token:
        await websocket.close(code=1013, reason="Runner internal API is disabled")
        return
    upstream_url = _websocket_url(
        settings.runner_internal_url,
        f"/internal/servers/{server_id}/terminal",
    )
    try:
        async with connect(
            upstream_url,
            additional_headers={
                "Authorization": f"Bearer {settings.internal_token}",
                "X-DevOps-Actor": ticket.username,
            },
            max_size=1024 * 1024,
            open_timeout=10,
            close_timeout=5,
        ) as upstream:
            await websocket.accept()

            async def browser_to_runner() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def runner_to_browser() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(browser_to_runner()),
                asyncio.create_task(runner_to_browser()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except (ConnectionClosed, OSError, TimeoutError, WebSocketDisconnect):
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(code=1011, reason="Runner terminal unavailable")

async def _runner_json(
    request: Request,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """调用 Runner 内部 HTTP 接口并统一转换网络、状态码和响应格式错误。"""
    settings = request.app.state.settings
    if not settings.internal_token:
        raise HTTPException(status_code=503, detail="Runner internal API is disabled")
    url = f"{settings.runner_internal_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {settings.internal_token}"}
    if actor:
        headers["X-DevOps-Actor"] = actor
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Runner is unavailable") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "Runner request failed")
        except ValueError:
            detail = "Runner request failed"
        raise HTTPException(status_code=response.status_code, detail=detail)
    value = response.json()
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Runner returned an invalid response")
    return value


def _websocket_url(base_url: str, path: str) -> str:
    """将 Runner HTTP 地址转换为对应的 WebSocket 协议地址。"""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return f"wss://{base[8:]}{path}"
    if base.startswith("http://"):
        return f"ws://{base[7:]}{path}"
    raise ValueError("runner_internal_url must use http or https")
