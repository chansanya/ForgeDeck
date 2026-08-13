"""提供手动流水线、运行状态、取消和有界 SSE 日志接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import (
    DeploymentEnvironment,
    PipelineRun,
    Project,
    RunLog,
    RunnerTask,
    RunStatus,
    TaskState,
)
from devops.schemas import PipelineRunRead, PipelineTrigger, RunLogRead
from devops.services import (
    add_audit,
    enqueue_pipeline,
    ensure_environment_ready,
    next_log_sequence,
)

router = APIRouter(tags=["pipelines"])


async def _require_run(session: SessionDep, run_id: str) -> PipelineRun:
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline run not found")
    return run


@router.get("/runs", response_model=list[PipelineRunRead])
async def list_runs(
    _: CurrentUser,
    session: SessionDep,
    project_id: str | None = None,
    run_status: RunStatus | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PipelineRun]:
    query = select(PipelineRun)
    if project_id:
        query = query.where(PipelineRun.project_id == project_id)
    if run_status:
        query = query.where(PipelineRun.status == run_status)
    query = query.order_by(PipelineRun.created_at.desc()).offset(offset).limit(limit)
    return list((await session.scalars(query)).all())


@router.get("/projects/{project_id}/runs", response_model=list[PipelineRunRead])
async def list_project_runs(
    project_id: str,
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PipelineRun]:
    if await session.get(Project, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    query = (
        select(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


@router.post(
    "/projects/{project_id}/runs",
    response_model=PipelineRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_run(
    project_id: str,
    payload: PipelineTrigger,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> PipelineRun:
    project = await session.get(Project, project_id)
    if project is None or not project.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if not project.image_repository:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Enabled project has no image_repository",
        )
    environment = None
    if payload.environment_id:
        environment = await session.get(DeploymentEnvironment, payload.environment_id)
        if environment is None or environment.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Environment does not belong to project",
            )
        try:
            await ensure_environment_ready(session, environment)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    run = await enqueue_pipeline(
        session,
        project=project,
        commit_sha=payload.commit_sha,
        ref=payload.ref,
        trigger_type="manual",
        trigger_actor=user.username,
        environment=environment,
    )
    await add_audit(
        session,
        actor=user.username,
        action="pipeline.trigger",
        resource_type="pipeline_run",
        resource_id=run.id,
        details={"project_id": project_id, "commit_sha": payload.commit_sha},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return run


@router.get("/runs/{run_id}", response_model=PipelineRunRead)
async def get_run(run_id: str, _: CurrentUser, session: SessionDep) -> PipelineRun:
    return await _require_run(session, run_id)


@router.get("/runs/{run_id}/logs", response_model=list[RunLogRead])
async def get_run_logs(
    run_id: str,
    _: CurrentUser,
    session: SessionDep,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[RunLog]:
    await _require_run(session, run_id)
    query = (
        select(RunLog)
        .where(RunLog.run_id == run_id, RunLog.sequence > after)
        .order_by(RunLog.sequence)
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


@router.post("/runs/{run_id}/cancel", response_model=PipelineRunRead)
async def cancel_run(
    run_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> PipelineRun:
    run = await _require_run(session, run_id)
    if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Run is already terminal")
    run.cancel_requested = True
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.CANCELLED
        await session.execute(
            update(RunnerTask)
            .where(RunnerTask.run_id == run.id, RunnerTask.state == TaskState.PENDING)
            .values(state=TaskState.CANCELLED, version=RunnerTask.version + 1)
        )
    sequence = await next_log_sequence(session, run.id)
    session.add(
        RunLog(
            run_id=run.id,
            sequence=sequence,
            level="warning",
            stage=run.current_stage,
            message="Cancellation requested",
        )
    )
    await add_audit(
        session,
        actor=user.username,
        action="pipeline.cancel",
        resource_type="pipeline_run",
        resource_id=run.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return run


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    _: CurrentUser,
    session: SessionDep,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    await _require_run(session, run_id)
    last_event_id = request.headers.get("last-event-id")
    cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)

    async def events() -> AsyncIterator[str]:
        nonlocal cursor
        idle_polls = 0
        settings = request.app.state.settings
        terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
        while True:
            if await request.is_disconnected():
                return
            async with request.app.state.database.session_factory() as poll_session:
                logs = list(
                    (
                        await poll_session.scalars(
                            select(RunLog)
                            .where(RunLog.run_id == run_id, RunLog.sequence > cursor)
                            .order_by(RunLog.sequence)
                            .limit(settings.sse_batch_size)
                        )
                    ).all()
                )
                current_status = await poll_session.scalar(
                    select(PipelineRun.status).where(PipelineRun.id == run_id)
                )
            if logs:
                idle_polls = 0
                for log in logs:
                    cursor = log.sequence
                    data = json.dumps(
                        RunLogRead.model_validate(log).model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {log.sequence}\nevent: log\ndata: {data}\n\n"
                continue
            idle_polls += 1
            if current_status in terminal:
                yield f"event: end\ndata: {json.dumps({'status': current_status.value})}\n\n"
                return
            if idle_polls % 20 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
