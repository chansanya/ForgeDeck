"""处理操作申请审批，并在参数哈希和资源快照复核后创建 Runner 任务。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select, update

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.db.results import affected_rows
from devops.domain.models import (
    ApprovalState,
    Deployment,
    DeploymentStatus,
    OperationKind,
    OperationRequest,
    PipelineRun,
    Project,
    RunLog,
    RunnerTask,
    TaskKind,
    utcnow,
)
from devops.schemas import ApprovalDecision, OperationRequestRead
from devops.services import add_audit, ensure_server_ready

router = APIRouter(prefix="/approvals", tags=["approvals"])


async def _require_operation(session: SessionDep, operation_id: str) -> OperationRequest:
    operation = await session.get(OperationRequest, operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
    return operation


@router.get("", response_model=list[OperationRequestRead])
async def list_approvals(
    _: CurrentUser,
    session: SessionDep,
    approval_state: ApprovalState | None = Query(default=None, alias="state"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[OperationRequest]:
    query = select(OperationRequest)
    if approval_state:
        query = query.where(OperationRequest.state == approval_state)
    query = query.order_by(OperationRequest.created_at.desc()).limit(limit)
    return list((await session.scalars(query)).all())


@router.get("/{operation_id}", response_model=OperationRequestRead)
async def get_approval(
    operation_id: str, _: CurrentUser, session: SessionDep
) -> OperationRequest:
    return await _require_operation(session, operation_id)


@router.post("/{operation_id}/approve", response_model=OperationRequestRead)
async def approve(
    operation_id: str,
    payload: ApprovalDecision,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> OperationRequest:
    operation = await _require_operation(session, operation_id)
    # 审批绑定申请创建时的不可变参数；constant-time 比较避免敏感哈希比较泄漏。
    if not hmac.compare_digest(operation.parameter_hash, payload.parameter_hash):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parameters changed; approval hash does not match",
        )
    now = utcnow()
    # 状态、哈希和过期时间放在同一个 UPDATE 中，避免重复点击或并发审批双重入队。
    result = await session.execute(
        update(OperationRequest)
        .where(
            OperationRequest.id == operation_id,
            OperationRequest.state == ApprovalState.PENDING,
            OperationRequest.parameter_hash == payload.parameter_hash,
            OperationRequest.expires_at > now,
        )
        .values(
            state=ApprovalState.APPROVED,
            approved_by=user.username,
            decided_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if affected_rows(result) != 1:
        await session.rollback()
        current = await session.get(OperationRequest, operation_id)
        if current and current.state == ApprovalState.PENDING:
            current.state = ApprovalState.EXPIRED
            await session.commit()
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Approval expired")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval already decided")

    parameters = operation.parameters
    if operation.kind in {OperationKind.DEPLOY, OperationKind.ROLLBACK}:
        environment_snapshot = parameters.get("environment_snapshot")
        if not isinstance(environment_snapshot, dict) or any(
            environment_snapshot.get(snapshot_key) != parameters.get(parameter_key)
            for snapshot_key, parameter_key in (
                ("id", "environment_id"),
                ("project_id", "project_id"),
                ("server_id", "server_id"),
            )
        ):
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Approved deployment target snapshot is inconsistent",
            )
    if operation.kind == OperationKind.ROLLBACK:
        # 审批可能滞后于线上状态变化，执行前必须重新确认当前版本和精确回滚目标。
        deployment = await session.get(Deployment, parameters.get("deployment_id"))
        target = await session.get(Deployment, parameters.get("target_deployment_id"))
        latest_healthy_id = (
            await session.scalar(
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
            if deployment is not None
            else None
        )
        if (
            deployment is None
            or target is None
            or deployment.status != DeploymentStatus.HEALTHY
            or latest_healthy_id != deployment.id
            or target.status != DeploymentStatus.HEALTHY
            or target.environment_id != deployment.environment_id
            or target.project_id != deployment.project_id
            or target.server_id != deployment.server_id
            or target.revision != parameters.get("target_revision")
            or target.image_ref != parameters.get("target_image_ref")
            or target.image_digest != parameters.get("target_image_digest")
            or target.compose_content != parameters.get("compose_content")
            or target.compose_sha256 != parameters.get("compose_sha256")
            or target.environment_snapshot != parameters.get("environment_snapshot")
            or deployment.previous_deployment_id != target.id
            or deployment.previous_revision != target.revision
        ):
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Approved rollback target no longer matches its immutable snapshot",
            )
    server_id = parameters.get("server_id")
    if operation.kind == OperationKind.BUILD:
        snapshot = parameters.get("config_snapshot")
        if isinstance(snapshot, dict):
            environment = snapshot.get("environment")
            if isinstance(environment, dict):
                server_id = environment.get("server_id")
    if operation.kind in {
        OperationKind.BUILD,
        OperationKind.DEPLOY,
        OperationKind.ROLLBACK,
        OperationKind.SCRIPT,
    } and server_id is not None:
        try:
            await ensure_server_ready(session, str(server_id))
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    task: RunnerTask
    if operation.kind == OperationKind.DEPLOY:
        deployment = Deployment(
            project_id=parameters["project_id"],
            environment_id=parameters["environment_id"],
            server_id=parameters["server_id"],
            image_ref=parameters["image_ref"],
            image_digest=parameters["image_digest"],
            revision=parameters["revision"],
            previous_revision=None,
            previous_deployment_id=None,
            compose_content=parameters["compose_content"],
            compose_sha256=parameters["compose_sha256"],
            environment_snapshot=parameters["environment_snapshot"],
        )
        session.add(deployment)
        await session.flush()
        task = RunnerTask(
            kind=TaskKind.DEPLOYMENT,
            resource_key=f"project:{parameters['project_id']}",
            deployment_id=deployment.id,
            operation_id=operation.id,
            payload={"action": "deploy", "deployment_id": deployment.id, **parameters},
        )
    elif operation.kind == OperationKind.ROLLBACK:
        task = RunnerTask(
            kind=TaskKind.DEPLOYMENT,
            resource_key=f"project:{parameters['project_id']}",
            deployment_id=parameters["deployment_id"],
            operation_id=operation.id,
            payload={"action": "rollback", **parameters},
        )
    elif operation.kind == OperationKind.SCRIPT:
        task = RunnerTask(
            kind=TaskKind.SCRIPT,
            resource_key=f"server:{parameters['server_id']}:script",
            operation_id=operation.id,
            payload=parameters,
            max_attempts=1,
        )
    elif operation.kind == OperationKind.BUILD:
        project = await session.get(Project, parameters["project_id"])
        if project is None:
            raise HTTPException(status_code=409, detail="Approved project no longer exists")
        snapshot = parameters.get("config_snapshot")
        snapshot_hash = parameters.get("snapshot_sha256")
        if not isinstance(snapshot, dict) or not isinstance(snapshot_hash, str):
            raise HTTPException(status_code=422, detail="Build request has no immutable snapshot")
        run = PipelineRun(
            project_id=project.id,
            environment_id=parameters.get("environment_id"),
            trigger_type="mcp",
            trigger_actor=operation.requested_by,
            provider="mcp",
            commit_sha=parameters["commit_sha"],
            ref=parameters["ref"],
            config_snapshot=snapshot,
            snapshot_sha256=snapshot_hash,
        )
        session.add(run)
        await session.flush()
        task = RunnerTask(
            kind=TaskKind.PIPELINE,
            resource_key=f"project:{parameters['project_id']}",
            operation_id=operation.id,
            run_id=run.id,
            payload={"run_id": run.id, **parameters},
        )
        session.add(RunLog(run_id=run.id, sequence=1, message="Pipeline queued", stage="queue"))
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown action")
    session.add(task)
    await session.flush()
    await add_audit(
        session,
        actor=user.username,
        action="approval.approve",
        resource_type="operation_request",
        resource_id=operation.id,
        details={"parameter_hash": operation.parameter_hash, "task_id": task.id},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    refreshed = await session.get(OperationRequest, operation.id, populate_existing=True)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approved operation no longer exists",
        )
    return refreshed


@router.post("/{operation_id}/reject", response_model=OperationRequestRead)
async def reject(
    operation_id: str,
    payload: ApprovalDecision,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> OperationRequest:
    operation = await _require_operation(session, operation_id)
    if not hmac.compare_digest(operation.parameter_hash, payload.parameter_hash):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval hash mismatch")
    result = await session.execute(
        update(OperationRequest)
        .where(
            OperationRequest.id == operation_id,
            OperationRequest.state == ApprovalState.PENDING,
        )
        .values(
            state=ApprovalState.REJECTED,
            approved_by=user.username,
            decided_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if affected_rows(result) != 1:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval already decided")
    await add_audit(
        session,
        actor=user.username,
        action="approval.reject",
        resource_type="operation_request",
        resource_id=operation.id,
        details={"parameter_hash": operation.parameter_hash},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    refreshed = await session.get(OperationRequest, operation.id, populate_existing=True)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rejected operation no longer exists",
        )
    return refreshed
