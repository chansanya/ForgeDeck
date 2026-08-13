"""管理加密通知通道并执行受控的连通性测试。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.api.deps import CurrentUser, SessionDep, client_ip, get_secret_manager
from devops.domain.models import NotificationChannel, NotificationKind, utcnow
from devops.integrations.notifications import Notification, send_notification
from devops.schemas import NotificationChannelCreate, NotificationChannelRead
from devops.services import add_audit

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationChannelRead])
async def list_channels(_: CurrentUser, session: SessionDep) -> list[NotificationChannel]:
    return list(
        (await session.scalars(select(NotificationChannel).order_by(NotificationChannel.name))).all()
    )


@router.post("", response_model=NotificationChannelRead, status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: NotificationChannelCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> NotificationChannel:
    config, target_hint = _validate_config(payload.kind, payload.config)
    secrets = get_secret_manager(request)
    channel = NotificationChannel(
        name=payload.name,
        kind=payload.kind,
        enabled=payload.enabled,
        events=sorted(set(payload.events)),
        encrypted_config=secrets.encrypt(
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ),
        target_hint=target_hint,
    )
    session.add(channel)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Notification name already exists") from exc
    await add_audit(
        session,
        actor=user.username,
        action="notification.create",
        resource_type="notification_channel",
        resource_id=channel.id,
        details={"kind": channel.kind.value, "events": channel.events},
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return channel


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, bool]:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    config = json.loads(get_secret_manager(request).decrypt(channel.encrypted_config))
    notification = Notification(
        event="notification.test",
        title="Light DevOps 通知测试",
        message="通知通道配置有效。",
        severity="info",
        resource_type="notification_channel",
        resource_id=channel.id,
    )
    try:
        await send_notification(channel.kind, config, notification)
    except Exception as exc:
        await add_audit(
            session,
            actor=user.username,
            action="notification.test",
            resource_type="notification_channel",
            resource_id=channel.id,
            outcome="failure",
            details={"error": type(exc).__name__},
            source_ip=client_ip(request),
            trace_id=request.state.trace_id,
        )
        await session.commit()
        raise HTTPException(status_code=502, detail="Notification delivery failed") from exc
    channel.last_tested_at = utcnow()
    await add_audit(
        session,
        actor=user.username,
        action="notification.test",
        resource_type="notification_channel",
        resource_id=channel.id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return {"ok": True}


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    await session.delete(channel)
    await add_audit(
        session,
        actor=user.username,
        action="notification.delete",
        resource_type="notification_channel",
        resource_id=channel_id,
        source_ip=client_ip(request),
        trace_id=request.state.trace_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _validate_config(
    kind: NotificationKind, value: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    config = dict(value)
    if kind == NotificationKind.DINGTALK:
        url = _required_string(config, "webhook_url")
        hint = _url_hint(url)
        if "secret" in config and not isinstance(config["secret"], str):
            raise HTTPException(status_code=422, detail="DingTalk secret must be a string")
        return config, hint
    if kind == NotificationKind.WEBHOOK:
        url = _required_string(config, "url")
        _required_string(config, "secret")
        return config, _url_hint(url)
    host = _required_string(config, "host")
    sender = _required_string(config, "sender")
    recipients = config.get("recipients")
    if not isinstance(recipients, list) or not recipients or not all(
        isinstance(item, str) and item for item in recipients
    ):
        raise HTTPException(status_code=422, detail="SMTP recipients must be a non-empty list")
    port = config.get("port", 587)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="SMTP port is invalid")
    config["port"] = port
    config["starttls"] = bool(config.get("starttls", True))
    return config, f"{sender} → {len(recipients)} recipient(s) via {host}:{port}"


def _required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"Notification config requires {key}")
    return value.strip()


def _url_hint(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Notification URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Notification URL must not include credentials")
    return f"{parsed.scheme}://{parsed.hostname}{':' + str(parsed.port) if parsed.port else ''}"
