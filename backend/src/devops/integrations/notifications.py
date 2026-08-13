"""实现钉钉、签名 Webhook 与 SMTP 通知发送和安全校验。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import smtplib
import ssl
import time
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devops.domain.models import NotificationChannel, NotificationKind
from devops.security import SecretManager
from devops.services import add_audit

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Notification:
    event: str
    title: str
    message: str
    severity: str = "info"
    resource_type: str | None = None
    resource_id: str | None = None
    url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NotificationDeliveryResult:
    channel_id: str
    channel_kind: NotificationKind
    delivered: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class _DeliveryTarget:
    channel_id: str
    channel_kind: NotificationKind
    encrypted_config: bytes


def canonical_payload(notification: Notification) -> bytes:
    return json.dumps(
        asdict(notification),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def signed_webhook_headers(payload: bytes, secret: str, timestamp: str) -> dict[str, str]:
    signature = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-DevOps-Timestamp": timestamp,
        "X-DevOps-Signature": f"sha256={signature}",
    }


def dingtalk_signed_url(webhook_url: str, secret: str, timestamp: str) -> str:
    parts = _validate_http_url(webhook_url)
    digest = hmac.new(
        secret.encode(),
        f"{timestamp}\n{secret}".encode(),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode()
    query = parts.query + ("&" if parts.query else "") + urlencode(
        {"timestamp": timestamp, "sign": signature}
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


async def send_signed_webhook(
    url: str,
    notification: Notification,
    *,
    secret: str,
    timeout_seconds: float = 10,
) -> None:
    _validate_http_url(url)
    payload = canonical_payload(notification)
    timestamp = str(int(time.time()))
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        response = await client.post(
            url,
            content=payload,
            headers=signed_webhook_headers(payload, secret, timestamp),
        )
        response.raise_for_status()


async def send_dingtalk(
    webhook_url: str,
    notification: Notification,
    *,
    secret: str | None = None,
    timeout_seconds: float = 10,
) -> None:
    _validate_http_url(webhook_url)
    url = webhook_url
    if secret:
        timestamp = str(int(time.time() * 1000))
        url = dingtalk_signed_url(webhook_url, secret, timestamp)

    lines = [f"### {notification.title}", "", notification.message]
    if notification.url:
        lines.extend(["", f"[查看详情]({notification.url})"])
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": notification.title, "text": "\n".join(lines)},
    }
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"DingTalk rejected notification: {result.get('errmsg', 'unknown')}")


async def send_smtp(
    notification: Notification,
    *,
    host: str,
    port: int,
    sender: str,
    recipients: list[str],
    username: str | None = None,
    password: str | None = None,
    starttls: bool = True,
    timeout_seconds: float = 15,
) -> None:
    if not recipients:
        raise ValueError("at least one SMTP recipient is required")
    message = EmailMessage()
    message["Subject"] = f"[{notification.severity.upper()}] {notification.title}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    body = notification.message
    if notification.url:
        body += f"\n\nDetails: {notification.url}"
    message.set_content(body)

    def deliver() -> None:
        with smtplib.SMTP(host, port, timeout=timeout_seconds) as client:
            client.ehlo()
            if starttls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if username:
                client.login(username, password or "")
            client.send_message(message)

    await asyncio.to_thread(deliver)


async def send_notification(
    kind: NotificationKind,
    config: dict[str, Any],
    notification: Notification,
) -> None:
    if kind == NotificationKind.DINGTALK:
        await send_dingtalk(
            config["webhook_url"], notification, secret=config.get("secret")
        )
    elif kind == NotificationKind.WEBHOOK:
        await send_signed_webhook(
            config["url"], notification, secret=config["secret"]
        )
    elif kind == NotificationKind.SMTP:
        await send_smtp(
            notification,
            host=config["host"],
            port=config["port"],
            sender=config["sender"],
            recipients=config["recipients"],
            username=config.get("username"),
            password=config.get("password"),
            starttls=config.get("starttls", True),
        )
    else:  # pragma: no cover - enum validation prevents this
        raise ValueError("unsupported notification kind")


async def deliver_event(
    session_factory: async_sessionmaker[AsyncSession],
    secret_manager: SecretManager,
    notification: Notification,
    *,
    actor: str = "system:notification",
    trace_id: str | None = None,
) -> tuple[NotificationDeliveryResult, ...]:
    """Deliver one domain event without allowing notification failures to escape.

    Channel configuration is decrypted only for the adapter invocation. Logs and
    audits contain channel identifiers and exception types, never configuration,
    notification bodies, or exception messages which may embed a target URL.
    """

    try:
        targets = await _load_delivery_targets(session_factory, notification.event)
    except Exception as exc:
        logger.error(
            "notification_channels_load_failed",
            notification_event=notification.event,
            resource_type=notification.resource_type,
            resource_id=notification.resource_id,
            error_type=type(exc).__name__,
        )
        return ()

    try:
        results = tuple(
            await asyncio.gather(
                *(
                    _deliver_to_target(target, secret_manager, notification)
                    for target in targets
                )
            )
        )
    except Exception as exc:
        logger.error(
            "notification_dispatch_failed",
            notification_event=notification.event,
            resource_type=notification.resource_type,
            resource_id=notification.resource_id,
            error_type=type(exc).__name__,
        )
        return ()
    await _record_delivery_results(
        session_factory,
        notification,
        results,
        actor=actor,
        trace_id=trace_id,
    )
    return results


async def _load_delivery_targets(
    session_factory: async_sessionmaker[AsyncSession], event: str
) -> tuple[_DeliveryTarget, ...]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    NotificationChannel.id,
                    NotificationChannel.kind,
                    NotificationChannel.events,
                    NotificationChannel.encrypted_config,
                ).where(NotificationChannel.enabled.is_(True))
            )
        ).all()
    return tuple(
        _DeliveryTarget(
            channel_id=channel_id,
            channel_kind=channel_kind,
            encrypted_config=encrypted_config,
        )
        for channel_id, channel_kind, events, encrypted_config in rows
        if not events or event in events or "*" in events
    )


async def _deliver_to_target(
    target: _DeliveryTarget,
    secret_manager: SecretManager,
    notification: Notification,
) -> NotificationDeliveryResult:
    try:
        config = json.loads(secret_manager.decrypt(target.encrypted_config))
        if not isinstance(config, dict):
            raise ValueError("notification configuration must be an object")
        await send_notification(target.channel_kind, config, notification)
    except Exception as exc:
        error_type = type(exc).__name__
        logger.warning(
            "notification_delivery_failed",
            notification_event=notification.event,
            channel_id=target.channel_id,
            channel_kind=target.channel_kind.value,
            resource_type=notification.resource_type,
            resource_id=notification.resource_id,
            error_type=error_type,
        )
        return NotificationDeliveryResult(
            channel_id=target.channel_id,
            channel_kind=target.channel_kind,
            delivered=False,
            error_type=error_type,
        )
    logger.info(
        "notification_delivered",
        notification_event=notification.event,
        channel_id=target.channel_id,
        channel_kind=target.channel_kind.value,
        resource_type=notification.resource_type,
        resource_id=notification.resource_id,
    )
    return NotificationDeliveryResult(
        channel_id=target.channel_id,
        channel_kind=target.channel_kind,
        delivered=True,
    )


async def _record_delivery_results(
    session_factory: async_sessionmaker[AsyncSession],
    notification: Notification,
    results: tuple[NotificationDeliveryResult, ...],
    *,
    actor: str,
    trace_id: str | None,
) -> None:
    if not results:
        return
    try:
        async with session_factory() as session:
            for result in results:
                details = {
                    "event": notification.event,
                    "channel_kind": result.channel_kind.value,
                    "subject_resource_type": notification.resource_type,
                    "subject_resource_id": notification.resource_id,
                }
                if result.error_type:
                    details["error_type"] = result.error_type
                await add_audit(
                    session,
                    actor=actor,
                    action="notification.deliver",
                    resource_type="notification_channel",
                    resource_id=result.channel_id,
                    outcome="success" if result.delivered else "failure",
                    details=details,
                    trace_id=trace_id,
                )
            await session.commit()
    except Exception as exc:
        logger.error(
            "notification_delivery_audit_failed",
            notification_event=notification.event,
            resource_type=notification.resource_type,
            resource_id=notification.resource_id,
            result_count=len(results),
            error_type=type(exc).__name__,
        )


def approval_pending_notification(
    *,
    operation_id: str,
    operation_kind: str,
    requested_by: str,
    parameter_hash: str,
) -> Notification:
    return Notification(
        event="approval.pending",
        title="有新的操作等待审批",
        message=f"{operation_kind} 操作申请 {operation_id} 正在等待管理员审批。",
        severity="warning",
        resource_type="operation_request",
        resource_id=operation_id,
        details={
            "operation_kind": operation_kind,
            "requested_by": requested_by,
            "parameter_hash": parameter_hash,
        },
    )


def run_result_notification(*, run_id: str, succeeded: bool) -> Notification:
    return Notification(
        event="run.succeeded" if succeeded else "run.failed",
        title="流水线运行成功" if succeeded else "流水线运行失败",
        message=(
            f"流水线运行 {run_id} 已成功完成。"
            if succeeded
            else f"流水线运行 {run_id} 执行失败，请查看运行日志。"
        ),
        severity="success" if succeeded else "error",
        resource_type="pipeline_run",
        resource_id=run_id,
        details={"status": "succeeded" if succeeded else "failed"},
    )


def deployment_result_notification(
    *, deployment_id: str, succeeded: bool, status: str
) -> Notification:
    return Notification(
        event="deployment.succeeded" if succeeded else "deployment.failed",
        title="部署成功" if succeeded else "部署失败",
        message=(
            f"部署 {deployment_id} 已成功完成。"
            if succeeded
            else f"部署 {deployment_id} 执行失败，请查看部署日志。"
        ),
        severity="success" if succeeded else "error",
        resource_type="deployment",
        resource_id=deployment_id,
        details={"status": status},
    )


def _validate_http_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("notification URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("notification URL must not contain credentials")
    return parsed
