"""实现单管理员登录、当前用户读取和密码修改接口。"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from devops.api.deps import CurrentUser, SessionDep, client_ip
from devops.domain.models import AdminUser, utcnow
from devops.schemas import LoginRequest, PasswordChange, TokenResponse, UserRead
from devops.security import DUMMY_PASSWORD_HASH, create_access_token, hash_password, verify_password
from devops.services import add_audit

router = APIRouter(prefix="/auth", tags=["auth"])
attempts: dict[str, deque] = defaultdict(deque)


def _rate_limit_key(request: Request, username: str) -> str:
    return f"{client_ip(request) or 'unknown'}:{username.lower()}"


def _check_rate_limit(key: str) -> None:
    now = utcnow()
    bucket = attempts[key]
    cutoff = now - timedelta(minutes=5)
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= 5:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts")
    bucket.append(now)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    key = _rate_limit_key(request, payload.username)
    _check_rate_limit(key)
    user = await session.scalar(select(AdminUser).where(AdminUser.username == payload.username))
    password_valid = verify_password(
        payload.password.get_secret_value(),
        user.password_hash if user is not None else DUMMY_PASSWORD_HASH,
    )
    if user is None or not user.is_active or not password_valid:
        await add_audit(
            session,
            actor=payload.username,
            action="auth.login",
            resource_type="session",
            outcome="denied",
            source_ip=client_ip(request),
            trace_id=request.state.trace_id,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    attempts.pop(key, None)
    settings = request.app.state.settings
    token, expires_in = create_access_token(
        subject=user.id,
        username=user.username,
        signing_key=request.app.state.secret_manager.signing_key,
        issuer=settings.jwt_issuer,
        expires_minutes=settings.access_token_minutes,
    )
    await add_audit(
        session,
        actor=user.username,
        action="auth.login",
        resource_type="session",
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> AdminUser:
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    if not verify_password(payload.current_password.get_secret_value(), user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid current password")
    user.password_hash = hash_password(payload.new_password.get_secret_value())
    user.password_changed_at = utcnow()
    await add_audit(
        session,
        actor=user.username,
        action="auth.password.change",
        resource_type="user",
        resource_id=user.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
