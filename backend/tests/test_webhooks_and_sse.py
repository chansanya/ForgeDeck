from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from devops.domain.models import PipelineRun, RunStatus, WebhookDelivery


@pytest.mark.parametrize("provider", ["github", "gitlab", "gitee"])
async def test_signed_webhook_is_deduplicated(
    provider: str, app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    secret = "webhook-secret"
    credential = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={"name": f"{provider}-hook", "kind": "webhook", "secret": secret},
    )
    project = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": f"{provider}-project",
            "repo_url": "https://git.example.com/acme/demo.git",
            "default_branch": "main",
            "webhook_credential_id": credential.json()["id"],
            "image_repository": "registry.example.com/acme/demo",
        },
    )
    assert project.status_code == 201, project.text
    payload = {
        "ref": "refs/heads/main",
        "after": "b" * 40,
        "checkout_sha": "b" * 40,
        "project": {"git_http_url": "https://git.example.com/acme/demo.git"},
        "repository": {"clone_url": "https://git.example.com/acme/demo.git"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"content-type": "application/json"}
    if provider == "github":
        headers.update(
            {
                "x-github-event": "push",
                "x-github-delivery": "delivery-1",
                "x-hub-signature-256": "sha256="
                + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            }
        )
    elif provider == "gitlab":
        headers.update(
            {
                "x-gitlab-event": "Push Hook",
                "x-gitlab-event-uuid": "delivery-1",
                "x-gitlab-token": secret,
            }
        )
    else:
        headers.update(
            {
                "x-gitee-event": "Push Hook",
                "x-gitee-delivery": "delivery-1",
                "x-gitee-token": secret,
            }
        )

    first = await client.post(f"/webhooks/{provider}", content=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "accepted"
    second = await client.post(f"/webhooks/{provider}", content=body, headers=headers)
    assert second.status_code == 202
    assert second.json() == {"status": "duplicate", "run_id": first.json()["run_id"]}

    async with app.state.database.session_factory() as session:
        deliveries = list((await session.scalars(select(WebhookDelivery))).all())
        assert len(deliveries) == 1


async def test_sse_replays_logs_and_ends_for_terminal_run(
    app, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    project = await client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={
            "name": "sse-project",
            "repo_url": "https://example.com/sse.git",
            "image_repository": "registry.example.com/sse",
        },
    )
    run = await client.post(
        f"/api/v1/projects/{project.json()['id']}/runs",
        headers=auth_headers,
        json={"commit_sha": "c" * 40, "ref": "refs/heads/main"},
    )
    async with app.state.database.session_factory() as session:
        entity = await session.get(PipelineRun, run.json()["id"])
        entity.status = RunStatus.SUCCEEDED
        await session.commit()

    stream = await client.get(
        f"/api/v1/runs/{run.json()['id']}/events", headers=auth_headers
    )
    assert stream.status_code == 200
    assert "event: log" in stream.text
    assert "Pipeline queued" in stream.text
    assert "event: end" in stream.text
