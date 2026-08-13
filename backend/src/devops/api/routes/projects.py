"""管理项目、Dockerfile 配置和绑定服务器的部署环境。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import Credential, DeploymentEnvironment, Project, Server
from devops.schemas import (
    EnvironmentCreate,
    EnvironmentRead,
    EnvironmentUpdate,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
)
from devops.services import add_audit

router = APIRouter(prefix="/projects", tags=["projects"])


async def _require_project(session: SessionDep, project_id: str) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _validate_project_references(
    session: SessionDep,
    git_credential_id: str | None,
    webhook_credential_id: str | None,
    registry_credential_id: str | None,
) -> None:
    for identifier, allowed in (
        (git_credential_id, "git"),
        (webhook_credential_id, "webhook"),
        (registry_credential_id, "registry"),
    ):
        if identifier is None:
            continue
        credential = await session.get(Credential, identifier)
        if credential is None or credential.kind.value != allowed:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{allowed} credential is invalid",
            )


async def _validate_project_state(session: SessionDep, project: Project) -> None:
    if project.enabled and not project.image_repository:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="image_repository is required for an enabled project",
        )
    default_environment_id = project.pipeline_config.get("default_environment_id")
    if default_environment_id is None:
        return
    environment = await session.get(DeploymentEnvironment, default_environment_id)
    if environment is None or environment.project_id != project.id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Default environment does not belong to project",
        )


async def _ensure_deploy_target_available(
    session: SessionDep,
    *,
    server_id: str,
    deploy_path: str,
    exclude_environment_id: str | None = None,
) -> None:
    query = select(DeploymentEnvironment.id).where(
        DeploymentEnvironment.server_id == server_id,
        DeploymentEnvironment.deploy_path == deploy_path,
    )
    if exclude_environment_id is not None:
        query = query.where(DeploymentEnvironment.id != exclude_environment_id)
    if await session.scalar(query.limit(1)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deploy path is already assigned on this server",
        )


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    _: CurrentUser,
    session: SessionDep,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Project]:
    query = select(Project).order_by(Project.name).offset(offset).limit(limit)
    return list((await session.scalars(query)).all())


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Project:
    await _validate_project_references(
        session,
        payload.git_credential_id,
        payload.webhook_credential_id,
        payload.registry_credential_id,
    )
    project = Project(**payload.model_dump())
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await _validate_project_state(session, project)
    await add_audit(
        session,
        actor=user.username,
        action="project.create",
        resource_type="project",
        resource_id=project.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return project


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: str, _: CurrentUser, session: SessionDep) -> Project:
    return await _require_project(session, project_id)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Project:
    project = await _require_project(session, project_id)
    values = payload.model_dump(exclude_unset=True)
    await _validate_project_references(
        session,
        values.get("git_credential_id", project.git_credential_id),
        values.get("webhook_credential_id", project.webhook_credential_id),
        values.get("registry_credential_id", project.registry_credential_id),
    )
    for key, value in values.items():
        setattr(project, key, value)
    if project.dockerfile_source == "inline" and not project.dockerfile_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="dockerfile_content is required for inline source",
        )
    await _validate_project_state(session, project)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="project.update",
        resource_type="project",
        resource_id=project.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    project = await _require_project(session, project_id)
    await session.delete(project)
    await add_audit(
        session,
        actor=user.username,
        action="project.delete",
        resource_type="project",
        resource_id=project_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project has run history") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/environments", response_model=list[EnvironmentRead])
async def list_environments(
    project_id: str, _: CurrentUser, session: SessionDep
) -> list[DeploymentEnvironment]:
    await _require_project(session, project_id)
    query = (
        select(DeploymentEnvironment)
        .where(DeploymentEnvironment.project_id == project_id)
        .order_by(DeploymentEnvironment.name)
    )
    return list((await session.scalars(query)).all())


@router.post(
    "/{project_id}/environments",
    response_model=EnvironmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_environment(
    project_id: str,
    payload: EnvironmentCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> DeploymentEnvironment:
    await _require_project(session, project_id)
    if await session.get(Server, payload.server_id) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid server")
    await _ensure_deploy_target_available(
        session,
        server_id=payload.server_id,
        deploy_path=payload.deploy_path,
    )
    environment = DeploymentEnvironment(project_id=project_id, **payload.model_dump())
    session.add(environment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Environment name or server deploy path already exists",
        ) from exc
    await add_audit(
        session,
        actor=user.username,
        action="environment.create",
        resource_type="environment",
        resource_id=environment.id,
        details={"project_id": project_id},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return environment


@router.patch("/{project_id}/environments/{environment_id}", response_model=EnvironmentRead)
async def update_environment(
    project_id: str,
    environment_id: str,
    payload: EnvironmentUpdate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> DeploymentEnvironment:
    environment = await session.get(DeploymentEnvironment, environment_id)
    if environment is None or environment.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    values = payload.model_dump(exclude_unset=True)
    if values.get("server_id") and await session.get(Server, values["server_id"]) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid server")
    await _ensure_deploy_target_available(
        session,
        server_id=str(values.get("server_id", environment.server_id)),
        deploy_path=str(values.get("deploy_path", environment.deploy_path)),
        exclude_environment_id=environment.id,
    )
    for key, value in values.items():
        setattr(environment, key, value)
    if environment.compose_source == "inline" and not environment.compose_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="compose_content is required for inline source",
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Environment name or server deploy path already exists",
        ) from exc
    await add_audit(
        session,
        actor=user.username,
        action="environment.update",
        resource_type="environment",
        resource_id=environment.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return environment


@router.delete(
    "/{project_id}/environments/{environment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_environment(
    project_id: str,
    environment_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    project = await _require_project(session, project_id)
    environment = await session.get(DeploymentEnvironment, environment_id)
    if environment is None or environment.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    if project.pipeline_config.get("default_environment_id") == environment_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Environment is configured as the project's default",
        )
    await session.delete(environment)
    await add_audit(
        session,
        actor=user.username,
        action="environment.delete",
        resource_type="environment",
        resource_id=environment_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Environment has deployment history"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
