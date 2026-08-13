"""签发、列出和吊销短期且带 scope 的 MCP Bearer Token。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import MCPAccessToken, utcnow
from devops.schemas import MCPTokenCreate, MCPTokenRead
from devops.services import add_audit

router = APIRouter(prefix="/mcp/tokens", tags=["mcp"])


@router.get("", response_model=list[MCPTokenRead])
async def list_tokens(_: CurrentUser, session: SessionDep) -> list[MCPAccessToken]:
    return list(
        (await session.scalars(select(MCPAccessToken).order_by(MCPAccessToken.created_at.desc()))).all()
    )


@router.post("", response_model=MCPTokenRead, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: MCPTokenCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> MCPTokenRead:
    raw_token = secrets.token_urlsafe(48)
    token = MCPAccessToken(
        name=payload.name,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        scopes=sorted(set(payload.scopes)),
        expires_at=utcnow() + timedelta(seconds=payload.expires_in_seconds),
    )
    session.add(token)
    await session.flush()
    await add_audit(
        session,
        actor=user.username,
        action="mcp.token.create",
        resource_type="mcp_token",
        resource_id=token.id,
        details={"scopes": token.scopes, "expires_at": token.expires_at.isoformat()},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return MCPTokenRead.model_validate(token).model_copy(update={"token": raw_token})


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    token = await session.get(MCPAccessToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="MCP token not found")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
    await add_audit(
        session,
        actor=user.username,
        action="mcp.token.revoke",
        resource_type="mcp_token",
        resource_id=token.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
