"""定义数据库会话、当前管理员、配置和主密钥等 FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from devops.config import Settings
from devops.domain.models import AdminUser
from devops.security import SecretManager, decode_access_token

bearer = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    """从应用状态提供当前请求使用的配置对象。"""
    return request.app.state.settings


def get_secret_manager(request: Request) -> SecretManager:
    """提供已初始化主密钥管理器，缺失时拒绝需要凭据的请求。"""
    return request.app.state.secret_manager


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为请求创建短生命周期数据库会话，并在异常时回滚。"""
    async with request.app.state.database.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUser:
    """解析 Bearer JWT 并加载管理员，统一处理令牌失效和账户不存在。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    settings: Settings = request.app.state.settings
    secrets: SecretManager = request.app.state.secret_manager
    try:
        payload = decode_access_token(
            credentials.credentials,
            signing_key=secrets.signing_key,
            issuer=settings.jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    user = await session.get(AdminUser, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return user


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[AdminUser, Depends(get_current_user)]


def client_ip(request: Request) -> str | None:
    """提取审计所需客户端地址，保留现有代理转发头兼容行为。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None
