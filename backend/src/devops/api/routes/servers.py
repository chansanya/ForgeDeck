"""登记目标服务器、确认 SSH 主机密钥并查询最近主机指标。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.api.routes.runner_proxy import _runner_json
from devops.domain.models import Credential, HostMetric, Server, utcnow
from devops.schemas import (
    HostKeyScanRead,
    HostKeyScanRequest,
    HostMetricRead,
    ServerCreate,
    ServerRead,
    ServerUpdate,
)
from devops.services import add_audit

router = APIRouter(prefix="/servers", tags=["servers"])


async def _require_server(session: SessionDep, server_id: str) -> Server:
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return server


async def _validate_ssh_credential(session: SessionDep, credential_id: str | None) -> None:
    if credential_id is None:
        return
    credential = await session.get(Credential, credential_id)
    if credential is None or credential.kind.value != "ssh":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="SSH credential is invalid",
        )


@router.get("", response_model=list[ServerRead])
async def list_servers(_: CurrentUser, session: SessionDep) -> list[Server]:
    return list((await session.scalars(select(Server).order_by(Server.name))).all())


@router.post("", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: ServerCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Server:
    await _validate_ssh_credential(session, payload.ssh_credential_id)
    server = Server(**payload.model_dump())
    session.add(server)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="server.create",
        resource_type="server",
        resource_id=server.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return server


@router.post("/host-key-scan", response_model=HostKeyScanRead)
async def scan_host_key(
    payload: HostKeyScanRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> HostKeyScanRead:
    result = await _runner_json(
        request,
        "POST",
        "/internal/ssh/host-key/scan",
        json=payload.model_dump(),
        actor=user.username,
    )
    scanned = HostKeyScanRead.model_validate(result)
    await add_audit(
        session,
        actor=user.username,
        action="server.host_key.scan",
        resource_type="ssh_host",
        resource_id=f"{payload.host}:{payload.port}",
        details={"algorithm": scanned.algorithm, "fingerprint": scanned.fingerprint},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return scanned


@router.get("/{server_id}", response_model=ServerRead)
async def get_server(server_id: str, _: CurrentUser, session: SessionDep) -> Server:
    return await _require_server(session, server_id)


@router.patch("/{server_id}", response_model=ServerRead)
async def update_server(
    server_id: str,
    payload: ServerUpdate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Server:
    server = await _require_server(session, server_id)
    values = payload.model_dump(exclude_unset=True)
    await _validate_ssh_credential(session, values.get("ssh_credential_id", server.ssh_credential_id))
    location_changed = (
        ("host" in values and values["host"] != server.host)
        or ("port" in values and values["port"] != server.port)
    )
    if location_changed and "host_key" not in values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Changing host or port requires an explicit host_key",
        )
    for key, value in values.items():
        setattr(server, key, value)
    if server.enabled and not server.host_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="host_key is required for an enabled server",
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="server.update",
        resource_type="server",
        resource_id=server.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return server


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    server = await _require_server(session, server_id)
    await session.delete(server)
    await add_audit(
        session,
        actor=user.username,
        action="server.delete",
        resource_type="server",
        resource_id=server_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Server is still in use") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{server_id}/metrics", response_model=list[HostMetricRead])
async def get_server_metrics(
    server_id: str,
    _: CurrentUser,
    session: SessionDep,
    hours: int = Query(default=1, ge=1, le=24),
    limit: int = Query(default=240, ge=1, le=3000),
) -> list[HostMetric]:
    await _require_server(session, server_id)
    query = (
        select(HostMetric)
        .where(
            HostMetric.server_id == server_id,
            HostMetric.collected_at >= utcnow() - timedelta(hours=hours),
        )
        .order_by(HostMetric.collected_at.desc())
        .limit(limit)
    )
    return list(reversed((await session.scalars(query)).all()))
