from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from devops.domain.models import (
    AuditEvent,
    NotificationChannel,
    NotificationKind,
    PipelineRun,
    Project,
    RunnerTask,
    TaskKind,
    TaskState,
)
from devops.integrations import notifications
from devops.integrations.notifications import (
    Notification,
    approval_pending_notification,
    canonical_payload,
    deployment_result_notification,
    dingtalk_signed_url,
    run_result_notification,
    signed_webhook_headers,
)
from devops.runner.store import SQLAlchemyRunnerTaskStore


def test_signed_webhook_uses_canonical_payload() -> None:
    notification = Notification(
        event="pipeline.failed",
        title="Build failed",
        message="Run 42 failed",
        details={"run_id": "42"},
    )
    payload = canonical_payload(notification)
    headers = signed_webhook_headers(payload, "secret", "1700000000")
    expected = hmac.new(
        b"secret",
        b"1700000000." + payload,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-DevOps-Signature"] == f"sha256={expected}"


def test_dingtalk_signature_is_url_encoded_once() -> None:
    url = dingtalk_signed_url(
        "https://oapi.dingtalk.com/robot/send?access_token=test",
        "secret",
        "1700000000000",
    )
    query = parse_qs(urlsplit(url).query)
    assert query["timestamp"] == ["1700000000000"]
    assert len(query["sign"][0]) > 20


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/hook", "https://user:pass@example.com/hook"],
)
def test_notification_url_rejects_unsafe_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        dingtalk_signed_url(url, "secret", "1700000000000")


def test_domain_notifications_use_stable_event_names() -> None:
    generated = (
        approval_pending_notification(
            operation_id="operation-1",
            operation_kind="deploy",
            requested_by="mcp",
            parameter_hash="a" * 64,
        ),
        run_result_notification(run_id="run-1", succeeded=True),
        run_result_notification(run_id="run-2", succeeded=False),
        deployment_result_notification(
            deployment_id="deployment-1", succeeded=True, status="healthy"
        ),
        deployment_result_notification(
            deployment_id="deployment-2", succeeded=False, status="rolled_back"
        ),
    )
    assert [item.event for item in generated] == [
        "approval.pending",
        "run.succeeded",
        "run.failed",
        "deployment.succeeded",
        "deployment.failed",
    ]


async def test_event_delivery_filters_channels_and_audits_without_plaintext(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_manager = app.state.secret_manager
    secret = "never-write-this-webhook-secret"
    encrypted = secret_manager.encrypt(
        json.dumps(
            {"url": "https://hooks.example.test/devops", "secret": secret},
            separators=(",", ":"),
        )
    )
    async with app.state.database.session_factory() as session:
        matching = NotificationChannel(
            name="matching",
            kind=NotificationKind.WEBHOOK,
            events=["run.failed"],
            encrypted_config=encrypted,
        )
        wildcard = NotificationChannel(
            name="wildcard",
            kind=NotificationKind.WEBHOOK,
            events=["*"],
            encrypted_config=encrypted,
        )
        session.add_all(
            (
                matching,
                wildcard,
                NotificationChannel(
                    name="other-event",
                    kind=NotificationKind.WEBHOOK,
                    events=["run.succeeded"],
                    encrypted_config=encrypted,
                ),
                NotificationChannel(
                    name="disabled",
                    kind=NotificationKind.WEBHOOK,
                    enabled=False,
                    events=["run.failed"],
                    encrypted_config=encrypted,
                ),
            )
        )
        await session.commit()
        expected_ids = {matching.id, wildcard.id}

    delivered: list[tuple[NotificationKind, dict[str, object], str]] = []

    async def fake_send(
        kind: NotificationKind,
        config: dict[str, object],
        notification: Notification,
    ) -> None:
        delivered.append((kind, config, notification.event))

    monkeypatch.setattr(notifications, "send_notification", fake_send)
    results = await notifications.deliver_event(
        app.state.database.session_factory,
        secret_manager,
        run_result_notification(run_id="run-42", succeeded=False),
        trace_id="trace-42",
    )

    assert {result.channel_id for result in results} == expected_ids
    assert all(result.delivered for result in results)
    assert len(delivered) == 2
    assert all(item[1]["secret"] == secret for item in delivered)
    async with app.state.database.session_factory() as session:
        audits = list(
            (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "notification.deliver",
                        AuditEvent.resource_id.in_(expected_ids),
                    )
                )
            ).all()
        )
    assert len(audits) == 2
    assert all(audit.outcome == "success" for audit in audits)
    assert all(audit.trace_id == "trace-42" for audit in audits)
    assert secret not in json.dumps([audit.details for audit in audits], ensure_ascii=False)


async def test_delivery_failure_is_isolated_and_audited_without_exception_message(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_manager = app.state.secret_manager
    secret = "plaintext-must-not-reach-audit"
    async with app.state.database.session_factory() as session:
        channel = NotificationChannel(
            name="failing",
            kind=NotificationKind.WEBHOOK,
            events=["deployment.failed"],
            encrypted_config=secret_manager.encrypt(
                json.dumps(
                    {"url": "https://hooks.example.test/fail", "secret": secret},
                    separators=(",", ":"),
                )
            ),
        )
        session.add(channel)
        await session.commit()
        channel_id = channel.id

    async def failing_send(*_args, **_kwargs) -> None:
        raise RuntimeError(f"adapter exposed {secret}")

    monkeypatch.setattr(notifications, "send_notification", failing_send)
    results = await notifications.deliver_event(
        app.state.database.session_factory,
        secret_manager,
        deployment_result_notification(
            deployment_id="deployment-42", succeeded=False, status="failed"
        ),
    )

    assert len(results) == 1
    assert results[0].channel_id == channel_id
    assert not results[0].delivered
    assert results[0].error_type == "RuntimeError"
    async with app.state.database.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "notification.deliver",
                AuditEvent.resource_id == channel_id,
            )
        )
    assert audit is not None
    assert audit.outcome == "failure"
    assert audit.details["error_type"] == "RuntimeError"
    assert secret not in json.dumps(audit.details, ensure_ascii=False)


@pytest.mark.parametrize(
    ("task_state", "expected_event"),
    ((TaskState.SUCCEEDED, "run.succeeded"), (TaskState.FAILED, "run.failed")),
)
async def test_runner_store_publishes_terminal_run_event(
    app,
    monkeypatch: pytest.MonkeyPatch,
    task_state: TaskState,
    expected_event: str,
) -> None:
    async with app.state.database.session_factory() as session:
        project = Project(name=f"project-{task_state.value}", repo_url="https://example.test/repo.git")
        session.add(project)
        await session.flush()
        run = PipelineRun(
            project_id=project.id,
            trigger_type="test",
            commit_sha="a" * 40,
            ref="refs/heads/main",
            config_snapshot={},
            snapshot_sha256="0" * 64,
        )
        session.add(run)
        await session.flush()
        session.add(
            RunnerTask(
                kind=TaskKind.PIPELINE,
                resource_key=f"project:{project.id}:build",
                payload={"run_id": run.id},
                run_id=run.id,
            )
        )
        await session.commit()

    published: list[Notification] = []

    async def capture_event(_factory, _secrets, notification: Notification, **_kwargs):
        published.append(notification)
        return ()

    monkeypatch.setattr("devops.runner.store.deliver_event", capture_event)
    store = SQLAlchemyRunnerTaskStore(
        app.state.database.session_factory, app.state.secret_manager
    )
    lease = await store.claim_next(worker_id="worker-1", lease_seconds=30)
    assert lease is not None
    running = await store.mark_running(lease)
    assert running is not None
    assert await store.finish(running, state=task_state)
    assert [item.event for item in published] == [expected_event]


async def test_api_request_publishes_approval_pending(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[Notification] = []

    async def capture_event(_factory, _secrets, notification: Notification, **_kwargs):
        published.append(notification)
        return ()

    monkeypatch.setattr("devops.api.routes.scripts.deliver_event", capture_event)
    credential = await client.post(
        "/api/v1/credentials",
        headers=auth_headers,
        json={"name": "notify-ssh", "kind": "ssh", "secret": "ssh-password"},
    )
    server = await client.post(
        "/api/v1/servers",
        headers=auth_headers,
        json={
            "name": "notify-node",
            "host": "10.0.0.30",
            "username": "deploy",
            "ssh_credential_id": credential.json()["id"],
            "host_key": "SHA256:test-host-key",
        },
    )
    script = await client.post(
        "/api/v1/scripts",
        headers=auth_headers,
        json={"name": "notify-script", "content": "docker compose ps"},
    )
    response = await client.post(
        f"/api/v1/scripts/{script.json()['id']}/executions",
        headers=auth_headers,
        json={"server_id": server.json()["id"], "arguments": {}},
    )

    assert response.status_code == 202, response.text
    assert [item.event for item in published] == ["approval.pending"]
    assert published[0].resource_id == response.json()["id"]
