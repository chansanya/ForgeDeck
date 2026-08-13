"""接收 GitLab、GitHub 与 Gitee Webhook，完成验签、过滤和幂等入队。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from devops.domain.models import (
    Credential,
    DeploymentEnvironment,
    Project,
    WebhookDelivery,
)
from devops.services import enqueue_pipeline, ensure_environment_ready

router = APIRouter(tags=["webhooks"])
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
COMMIT_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _normalize_repo_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


def _repository_urls(provider: str, payload: dict[str, Any]) -> list[str]:
    candidates: list[Any]
    if provider == "gitlab":
        candidates = [
            _nested(payload, "project", "git_http_url"),
            _nested(payload, "project", "git_ssh_url"),
            _nested(payload, "repository", "git_http_url"),
        ]
    elif provider == "github":
        candidates = [
            _nested(payload, "repository", "clone_url"),
            _nested(payload, "repository", "ssh_url"),
            _nested(payload, "repository", "html_url"),
        ]
    else:
        candidates = [
            _nested(payload, "project", "git_http_url"),
            _nested(payload, "repository", "clone_url"),
            _nested(payload, "repository", "ssh_url"),
            _nested(payload, "repository", "url"),
        ]
    return [_normalize_repo_url(value) for value in candidates if isinstance(value, str)]


def _event_type(provider: str, request: Request) -> str:
    header = {
        "gitlab": "x-gitlab-event",
        "github": "x-github-event",
        "gitee": "x-gitee-event",
    }[provider]
    return request.headers.get(header, "")


def _is_push(provider: str, event_type: str) -> bool:
    lowered = event_type.lower()
    if provider == "github":
        return lowered == "push"
    return lowered in {"push hook", "push_hooks", "push"}


def _delivery_id(provider: str, request: Request, payload_sha: str) -> str:
    candidates = {
        "gitlab": ("x-gitlab-event-uuid", "x-gitlab-delivery"),
        "github": ("x-github-delivery",),
        "gitee": ("x-gitee-delivery",),
    }[provider]
    return next((request.headers[name] for name in candidates if request.headers.get(name)), payload_sha)


def _verify_signature(provider: str, request: Request, body: bytes, secret: str) -> bool:
    if provider == "github":
        provided = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)
    if provider == "gitlab":
        return hmac.compare_digest(request.headers.get("x-gitlab-token", ""), secret)
    return hmac.compare_digest(request.headers.get("x-gitee-token", ""), secret)


async def _handle(provider: str, request: Request) -> dict[str, Any]:
    """完成 Webhook 验签、事件过滤、delivery 去重和流水线入队。"""
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")

    payload_sha = hashlib.sha256(body).hexdigest()
    # Provider 未提供 delivery ID 时使用原始载荷哈希，仍可对完全相同的重投递去重。
    delivery_id = _delivery_id(provider, request, payload_sha)
    event_type = _event_type(provider, request)
    repo_urls = set(_repository_urls(provider, payload))
    if not repo_urls:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Repository is missing")

    async with request.app.state.database.session_factory() as session:
        duplicate = await session.scalar(
            select(WebhookDelivery).where(
                WebhookDelivery.provider == provider,
                WebhookDelivery.delivery_id == delivery_id,
            )
        )
        if duplicate:
            return {"status": "duplicate", "run_id": duplicate.run_id}

        projects = list(
            (
                await session.scalars(
                    select(Project).where(Project.enabled.is_(True), Project.webhook_credential_id.is_not(None))
                )
            ).all()
        )
        project = next(
            (item for item in projects if _normalize_repo_url(item.repo_url) in repo_urls),
            None,
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if not project.image_repository:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Enabled project has no image_repository",
            )
        credential = await session.get(Credential, project.webhook_credential_id)
        if credential is None or credential.kind.value != "webhook":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook not configured")
        secret = request.app.state.secret_manager.decrypt(credential.encrypted_secret)
        if not _verify_signature(provider, request, body, secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

        delivery = WebhookDelivery(
            provider=provider,
            delivery_id=delivery_id,
            project_id=project.id,
            payload_sha256=payload_sha,
            event_type=event_type,
            status="received",
        )
        session.add(delivery)
        try:
            await session.flush()
        except IntegrityError:
            # 预查询只优化常见路径，唯一约束才是并发请求下真正的幂等边界。
            await session.rollback()
            duplicate = await session.scalar(
                select(WebhookDelivery).where(
                    WebhookDelivery.provider == provider,
                    WebhookDelivery.delivery_id == delivery_id,
                )
            )
            return {"status": "duplicate", "run_id": duplicate.run_id if duplicate else None}

        if not _is_push(provider, event_type):
            delivery.status = "ignored_event"
            await session.commit()
            return {"status": "ignored", "reason": "event"}

        ref = payload.get("ref")
        commit_sha = payload.get("checkout_sha") or payload.get("after")
        if not isinstance(ref, str) or not isinstance(commit_sha, str):
            delivery.status = "invalid_payload"
            await session.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing ref or commit")
        if not COMMIT_SHA.fullmatch(commit_sha) or set(commit_sha) == {"0"}:
            delivery.status = "ignored_commit"
            await session.commit()
            return {"status": "ignored", "reason": "commit"}
        expected_ref = f"refs/heads/{project.default_branch}"
        if ref != expected_ref:
            delivery.status = "ignored_branch"
            await session.commit()
            return {"status": "ignored", "reason": "branch"}

        environment = None
        environment_id = project.pipeline_config.get("default_environment_id")
        if environment_id:
            environment = await session.get(DeploymentEnvironment, environment_id)
            if environment is None or environment.project_id != project.id:
                delivery.status = "invalid_environment"
                await session.commit()
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Default environment is invalid",
                )
            try:
                await ensure_environment_ready(session, environment)
            except ValueError as exc:
                delivery.status = "invalid_server"
                await session.commit()
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
        run = await enqueue_pipeline(
            session,
            project=project,
            commit_sha=commit_sha,
            ref=ref,
            trigger_type="webhook",
            trigger_actor=provider,
            environment=environment,
            provider=provider,
            delivery_id=delivery_id,
        )
        delivery.status = "accepted"
        delivery.run_id = run.id
        await session.commit()
        return {"status": "accepted", "run_id": run.id}


@router.post("/gitlab", status_code=status.HTTP_202_ACCEPTED)
async def gitlab(request: Request) -> dict[str, Any]:
    """处理 GitLab 推送 Webhook。"""
    return await _handle("gitlab", request)


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github(request: Request) -> dict[str, Any]:
    """处理 GitHub 推送 Webhook。"""
    return await _handle("github", request)


@router.post("/gitee", status_code=status.HTTP_202_ACCEPTED)
async def gitee(request: Request) -> dict[str, Any]:
    """处理 Gitee 推送 Webhook。"""
    return await _handle("gitee", request)
