"""管理版本化部署脚本，并创建需要审批的远程执行申请。"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.api.deps import CurrentUser, SessionDep, client_ip, get_secret_manager
from devops.domain.models import OperationKind, Script, ScriptVersion
from devops.integrations.notifications import (
    approval_pending_notification,
    deliver_event,
)
from devops.schemas import (
    OperationRequestRead,
    ScriptCreate,
    ScriptExecutionCreate,
    ScriptRead,
    ScriptUpdate,
)
from devops.services import add_audit, create_operation_request, ensure_server_ready

router = APIRouter(prefix="/scripts", tags=["scripts"])


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


async def _require_script(session: SessionDep, script_id: str) -> Script:
    script = await session.get(Script, script_id)
    if script is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")
    return script


async def _script_read(session: SessionDep, script: Script) -> ScriptRead:
    version = await session.scalar(
        select(ScriptVersion).where(
            ScriptVersion.script_id == script.id,
            ScriptVersion.version == script.current_version,
        )
    )
    if version is None:
        raise RuntimeError(f"Script {script.id} has no current version")
    return ScriptRead(
        id=script.id,
        name=script.name,
        description=script.description,
        enabled=script.enabled,
        current_version=script.current_version,
        content=version.content,
        sha256=version.sha256,
        created_at=script.created_at,
        updated_at=script.updated_at,
    )


@router.get("", response_model=list[ScriptRead])
async def list_scripts(_: CurrentUser, session: SessionDep) -> list[ScriptRead]:
    scripts = list((await session.scalars(select(Script).order_by(Script.name))).all())
    return [await _script_read(session, script) for script in scripts]


@router.post("", response_model=ScriptRead, status_code=status.HTTP_201_CREATED)
async def create_script(
    payload: ScriptCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> ScriptRead:
    script = Script(
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        current_version=1,
    )
    session.add(script)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    session.add(
        ScriptVersion(
            script_id=script.id,
            version=1,
            content=payload.content,
            sha256=_digest(payload.content),
        )
    )
    await add_audit(
        session,
        actor=user.username,
        action="script.create",
        resource_type="script",
        resource_id=script.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return await _script_read(session, script)


@router.get("/{script_id}", response_model=ScriptRead)
async def get_script(script_id: str, _: CurrentUser, session: SessionDep) -> ScriptRead:
    return await _script_read(session, await _require_script(session, script_id))


@router.patch("/{script_id}", response_model=ScriptRead)
async def update_script(
    script_id: str,
    payload: ScriptUpdate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> ScriptRead:
    script = await _require_script(session, script_id)
    values = payload.model_dump(exclude_unset=True, exclude={"content"})
    for key, value in values.items():
        setattr(script, key, value)
    if payload.content is not None:
        script.current_version += 1
        session.add(
            ScriptVersion(
                script_id=script.id,
                version=script.current_version,
                content=payload.content,
                sha256=_digest(payload.content),
            )
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="script.update",
        resource_type="script",
        resource_id=script.id,
        details={"version": script.current_version},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return await _script_read(session, script)


@router.delete("/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(
    script_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    script = await _require_script(session, script_id)
    await session.delete(script)
    await add_audit(
        session,
        actor=user.username,
        action="script.delete",
        resource_type="script",
        resource_id=script_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{script_id}/executions",
    response_model=OperationRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_script_execution(
    script_id: str,
    payload: ScriptExecutionCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
):
    script = await _require_script(session, script_id)
    if not script.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Script is disabled")
    try:
        server = await ensure_server_ready(session, payload.server_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    version = await session.scalar(
        select(ScriptVersion).where(
            ScriptVersion.script_id == script.id,
            ScriptVersion.version == script.current_version,
        )
    )
    if version is None:
        raise RuntimeError("Current script version is missing")
    parameters = {
        "script_id": script.id,
        "script_version_id": version.id,
        "script_version": version.version,
        "script_sha256": version.sha256,
        "server_id": server.id,
        "arguments": payload.arguments,
    }
    operation = await create_operation_request(
        session,
        kind=OperationKind.SCRIPT,
        requested_by=f"admin:{user.username}",
        parameters=parameters,
        preview={
            "action": "execute-approved-script",
            "script": script.name,
            "version": version.version,
            "server": server.name,
            "arguments": payload.arguments,
        },
    )
    await add_audit(
        session,
        actor=user.username,
        action="script.execution.request",
        resource_type="operation_request",
        resource_id=operation.id,
        details={"parameter_hash": operation.parameter_hash},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    await deliver_event(
        request.app.state.database.session_factory,
        get_secret_manager(request),
        approval_pending_notification(
            operation_id=operation.id,
            operation_kind=operation.kind.value,
            requested_by=operation.requested_by,
            parameter_hash=operation.parameter_hash,
        ),
        trace_id=request.state.trace_id,
    )
    return operation
