"""管理加密凭据元数据，接口始终屏蔽凭据明文。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import Credential
from devops.schemas import CredentialCreate, CredentialRead, CredentialUpdate
from devops.services import add_audit

router = APIRouter(prefix="/credentials", tags=["credentials"])


@router.get("", response_model=list[CredentialRead])
async def list_credentials(_: CurrentUser, session: SessionDep) -> list[Credential]:
    return list((await session.scalars(select(Credential).order_by(Credential.name))).all())


@router.post("", response_model=CredentialRead, status_code=status.HTTP_201_CREATED)
async def create_credential(
    payload: CredentialCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Credential:
    credential = Credential(
        name=payload.name,
        kind=payload.kind,
        encrypted_secret=request.app.state.secret_manager.encrypt(payload.secret.get_secret_value()),
        details=payload.metadata,
    )
    session.add(credential)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="credential.create",
        resource_type="credential",
        resource_id=credential.id,
        details={"kind": credential.kind.value},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return credential


@router.get("/{credential_id}", response_model=CredentialRead)
async def get_credential(
    credential_id: str, _: CurrentUser, session: SessionDep
) -> Credential:
    credential = await session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    return credential


@router.patch("/{credential_id}", response_model=CredentialRead)
async def update_credential(
    credential_id: str,
    payload: CredentialUpdate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Credential:
    credential = await session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    fields = payload.model_dump(exclude_unset=True, exclude={"secret", "metadata"})
    for key, value in fields.items():
        setattr(credential, key, value)
    if payload.metadata is not None:
        credential.details = payload.metadata
    if payload.secret is not None:
        credential.encrypted_secret = request.app.state.secret_manager.encrypt(
            payload.secret.get_secret_value()
        )
        credential.version += 1
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="credential.update",
        resource_type="credential",
        resource_id=credential.id,
        details={"secret_rotated": payload.secret is not None},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return credential


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    credential = await session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    await session.delete(credential)
    await add_audit(
        session,
        actor=user.username,
        action="credential.delete",
        resource_type="credential",
        resource_id=credential_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Credential is still referenced",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
