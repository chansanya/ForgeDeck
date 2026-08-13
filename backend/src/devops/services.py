"""提供配置快照、审计记录与流水线入队等跨路由业务服务。

本模块负责固化可复核的业务数据，不执行 Docker、SSH 等 Runner 副作用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from devops.domain.models import (
    AuditEvent,
    DeploymentEnvironment,
    OperationKind,
    OperationRequest,
    PipelineRun,
    Project,
    RunLog,
    RunnerTask,
    Server,
    TaskKind,
    utcnow,
)


def canonical_json(value: Any) -> str:
    """生成稳定 JSON 表示，供配置快照和审批参数计算一致哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    """对规范化 JSON 求 SHA-256，避免字典顺序造成审批哈希漂移。"""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def project_snapshot(project: Project, environment: DeploymentEnvironment | None) -> dict[str, Any]:
    """复制构建与部署配置，形成运行期间不可被后续编辑覆盖的快照。"""
    snapshot: dict[str, Any] = {
        "project": {
            "id": project.id,
            "name": project.name,
            "repo_url": project.repo_url,
            "default_branch": project.default_branch,
            "git_credential_id": project.git_credential_id,
            "registry_credential_id": project.registry_credential_id,
            "dockerfile_source": project.dockerfile_source,
            "dockerfile_path": project.dockerfile_path,
            "dockerfile_content": project.dockerfile_content,
            "build_context": project.build_context,
            "image_repository": project.image_repository,
            "build_args": project.build_args,
            "pipeline_config": project.pipeline_config,
        }
    }
    if environment:
        snapshot["environment"] = {
            "id": environment.id,
            "project_id": environment.project_id,
            "name": environment.name,
            "server_id": environment.server_id,
            "compose_source": environment.compose_source,
            "compose_path": environment.compose_path,
            "compose_content": environment.compose_content,
            "deploy_path": environment.deploy_path,
            "env_config": environment.env_config,
            "healthcheck": environment.healthcheck,
        }
    return snapshot


async def ensure_environment_ready(
    session: AsyncSession, environment: DeploymentEnvironment
) -> Server:
    """校验环境绑定的服务器可用，并复用统一 SSH 安全边界。"""
    return await ensure_server_ready(session, environment.server_id)


async def ensure_server_ready(session: AsyncSession, server_id: str) -> Server:
    """确认目标服务器启用、已登记主机指纹且具备 SSH 凭据。"""
    server = await session.get(Server, server_id)
    if server is None or not server.enabled:
        raise ValueError("target server does not exist or is disabled")
    if not server.host_key:
        raise ValueError("target server has no pinned SSH host key")
    if not server.ssh_credential_id:
        raise ValueError("target server has no SSH credential")
    return server


async def enqueue_pipeline(
    session: AsyncSession,
    *,
    project: Project,
    commit_sha: str,
    ref: str,
    trigger_type: str,
    trigger_actor: str | None,
    environment: DeploymentEnvironment | None = None,
    provider: str | None = None,
    delivery_id: str | None = None,
) -> PipelineRun:
    """固化 commit 和配置快照后创建流水线记录及持久 Runner 任务。"""
    snapshot = project_snapshot(project, environment)
    snapshot_hash = sha256_json(snapshot)
    run = PipelineRun(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        status="queued",
        trigger_type=trigger_type,
        trigger_actor=trigger_actor,
        provider=provider,
        delivery_id=delivery_id,
        commit_sha=commit_sha,
        ref=ref,
        config_snapshot=snapshot,
        snapshot_sha256=snapshot_hash,
    )
    session.add(run)
    await session.flush()
    session.add(
        RunnerTask(
            kind=TaskKind.PIPELINE,
            resource_key=f"project:{project.id}",
            run_id=run.id,
            payload={
                "run_id": run.id,
                "project_id": project.id,
                "environment_id": environment.id if environment else None,
                "commit_sha": commit_sha,
                "ref": ref,
                "config_snapshot": snapshot,
                "snapshot_sha256": snapshot_hash,
            },
        )
    )
    session.add(RunLog(run_id=run.id, sequence=1, message="Pipeline queued", stage="queue"))
    await session.flush()
    return run


async def create_operation_request(
    session: AsyncSession,
    *,
    kind: OperationKind,
    requested_by: str,
    parameters: dict[str, Any],
    preview: dict[str, Any],
    ttl_minutes: int = 30,
) -> OperationRequest:
    """创建带参数哈希和过期时间的审批申请，防止审批后参数被替换。"""
    request = OperationRequest(
        kind=kind,
        requested_by=requested_by,
        parameters=parameters,
        parameter_hash=sha256_json(parameters),
        preview=preview,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes),
    )
    session.add(request)
    await session.flush()
    return request


async def add_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    source_ip: str | None = None,
    trace_id: str | None = None,
    outcome: str = "success",
) -> AuditEvent:
    """追加不可变审计元数据；详情只允许保存已脱敏的业务信息。"""
    event = AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        source_ip=source_ip,
        trace_id=trace_id,
        outcome=outcome,
    )
    session.add(event)
    await session.flush()
    return event


async def next_log_sequence(session: AsyncSession, run_id: str) -> int:
    """为流水线日志计算下一个序号，供同一事务内追加事件。"""
    current = await session.scalar(select(func.max(RunLog.sequence)).where(RunLog.run_id == run_id))
    return int(current or 0) + 1
