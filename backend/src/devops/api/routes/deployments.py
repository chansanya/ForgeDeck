"""创建部署与精确回滚申请，并固化目标环境和镜像快照。"""

from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from devops.api.deps import CurrentUser, SessionDep, client_ip, get_secret_manager
from devops.domain.models import (
    Deployment,
    DeploymentEnvironment,
    DeploymentStatus,
    OperationKind,
    Project,
)
from devops.integrations.notifications import (
    approval_pending_notification,
    deliver_event,
)
from devops.schemas import DeploymentRead, DeploymentRequestCreate, OperationRequestRead
from devops.services import (
    add_audit,
    create_operation_request,
    ensure_environment_ready,
    ensure_server_ready,
)

router = APIRouter(prefix="/deployments", tags=["deployments"])
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@router.get("", response_model=list[DeploymentRead])
async def list_deployments(
    _: CurrentUser,
    session: SessionDep,
    project_id: str | None = None,
    environment_id: str | None = None,
    deployment_status: DeploymentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Deployment]:
    query = select(Deployment)
    if project_id:
        query = query.where(Deployment.project_id == project_id)
    if environment_id:
        query = query.where(Deployment.environment_id == environment_id)
    if deployment_status:
        query = query.where(Deployment.status == deployment_status)
    query = query.order_by(Deployment.created_at.desc()).limit(limit)
    return list((await session.scalars(query)).all())


@router.get("/{deployment_id}", response_model=DeploymentRead)
async def get_deployment(
    deployment_id: str, _: CurrentUser, session: SessionDep
) -> Deployment:
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return deployment


@router.post(
    "/requests",
    response_model=OperationRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_deployment(
    payload: DeploymentRequestCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
):
    environment = await session.get(DeploymentEnvironment, payload.environment_id)
    if environment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    try:
        await ensure_environment_ready(session, environment)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    project = await session.get(Project, environment.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project no longer exists")
    service_name = project.pipeline_config.get("service_name") or "app"
    if not isinstance(service_name, str) or not _SERVICE_NAME.fullmatch(service_name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Project pipeline_config.service_name is invalid",
        )
    min_free_bytes = _minimum_free_bytes(project.pipeline_config.get("min_free_bytes"))
    if environment.compose_source == "inline":
        compose_content = environment.compose_content
    else:
        compose_content = payload.compose_content
    if not compose_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Repository-sourced deployment requires an immutable compose_content snapshot",
        )
    compose_sha256 = hashlib.sha256(compose_content.encode()).hexdigest()
    environment_snapshot = {
        "id": environment.id,
        "project_id": environment.project_id,
        "server_id": environment.server_id,
        "name": environment.name,
        "deploy_path": environment.deploy_path,
        "compose_source": environment.compose_source,
        "compose_path": environment.compose_path,
        "compose_sha256": compose_sha256,
        "env_config": environment.env_config,
        "healthcheck": environment.healthcheck,
        "registry_credential_id": project.registry_credential_id,
        "service_name": service_name,
        "min_free_bytes": min_free_bytes,
    }
    parameters = {
        "environment_id": environment.id,
        "project_id": environment.project_id,
        "server_id": environment.server_id,
        "image_ref": payload.image_ref,
        "image_digest": payload.image_digest.lower(),
        "revision": payload.revision,
        "compose_content": compose_content,
        "compose_sha256": compose_sha256,
        "environment_snapshot": environment_snapshot,
        "registry_credential_id": project.registry_credential_id,
        "service_name": service_name,
        "min_free_bytes": min_free_bytes,
    }
    operation = await create_operation_request(
        session,
        kind=OperationKind.DEPLOY,
        requested_by=f"admin:{user.username}",
        parameters=parameters,
        preview={
            "action": "deploy",
            "target": environment.name,
            "server_id": environment.server_id,
            "immutable_image": f"{payload.image_ref}@{payload.image_digest.lower()}",
            "compose_sha256": compose_sha256,
            "service_name": service_name,
        },
    )
    await add_audit(
        session,
        actor=user.username,
        action="deployment.request",
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


@router.post(
    "/{deployment_id}/rollback-request",
    response_model=OperationRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_rollback(
    deployment_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
):
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    try:
        await ensure_server_ready(session, deployment.server_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    latest_healthy_id = await session.scalar(
        select(Deployment.id)
        .where(
            Deployment.environment_id == deployment.environment_id,
            Deployment.status == DeploymentStatus.HEALTHY,
        )
        .order_by(
            Deployment.finished_at.desc().nulls_last(),
            Deployment.created_at.desc(),
        )
        .limit(1)
    )
    if latest_healthy_id != deployment.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only the active healthy deployment can be rolled back",
        )
    if deployment.previous_deployment_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment has no exact previous deployment snapshot",
        )
    target = await session.get(Deployment, deployment.previous_deployment_id)
    if (
        target is None
        or target.status != DeploymentStatus.HEALTHY
        or target.environment_id != deployment.environment_id
        or target.project_id != deployment.project_id
        or target.server_id != deployment.server_id
        or target.revision != deployment.previous_revision
        or not target.compose_content
        or not target.compose_sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Previous revision has no immutable Compose snapshot",
        )
    target_snapshot = target.environment_snapshot
    current_snapshot = deployment.environment_snapshot
    if (
        current_snapshot.get("server_id") != deployment.server_id
        or current_snapshot.get("project_id") != deployment.project_id
        or current_snapshot.get("id") != deployment.environment_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Current deployment has an inconsistent target snapshot",
        )
    if (
        target_snapshot.get("server_id") != target.server_id
        or target_snapshot.get("project_id") != target.project_id
        or target_snapshot.get("id") != target.environment_id
        or target_snapshot.get("deploy_path") != current_snapshot.get("deploy_path")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Previous deployment target snapshot is inconsistent with the active target",
        )
    service_name = target_snapshot.get("service_name")
    if not isinstance(service_name, str) or not _SERVICE_NAME.fullmatch(service_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Previous revision has no immutable service_name snapshot",
        )
    registry_credential_id = target_snapshot.get("registry_credential_id")
    if registry_credential_id is not None and not isinstance(registry_credential_id, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Previous revision has an invalid registry credential snapshot",
        )
    try:
        min_free_bytes = _minimum_free_bytes(target_snapshot.get("min_free_bytes"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Previous revision has an invalid disk preflight snapshot",
        ) from exc
    rollback_snapshot = {
        **target_snapshot,
        "registry_credential_id": registry_credential_id,
        "service_name": service_name,
        "min_free_bytes": min_free_bytes,
    }
    parameters = {
        "deployment_id": deployment.id,
        "target_deployment_id": target.id,
        "environment_id": deployment.environment_id,
        "project_id": deployment.project_id,
        "server_id": deployment.server_id,
        "target_revision": target.revision,
        "target_image_ref": target.image_ref,
        "target_image_digest": target.image_digest,
        "compose_content": target.compose_content,
        "compose_sha256": target.compose_sha256,
        "environment_snapshot": rollback_snapshot,
        "registry_credential_id": registry_credential_id,
        "service_name": service_name,
        "min_free_bytes": min_free_bytes,
    }
    operation = await create_operation_request(
        session,
        kind=OperationKind.ROLLBACK,
        requested_by=f"admin:{user.username}",
        parameters=parameters,
        preview={
            "action": "rollback",
            "target_deployment_id": target.id,
            "target_revision": target.revision,
        },
    )
    await add_audit(
        session,
        actor=user.username,
        action="deployment.rollback.request",
        resource_type="operation_request",
        resource_id=operation.id,
        details={"deployment_id": deployment.id, "target_deployment_id": target.id},
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


def _minimum_free_bytes(value: object) -> int:
    if value is None:
        return 512 * 1024 * 1024
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("min_free_bytes must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError("min_free_bytes cannot be negative")
    return result
