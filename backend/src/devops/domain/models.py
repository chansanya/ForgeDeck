"""定义 API 与 Runner 共享的 SQLAlchemy 领域模型、枚举和持久状态。"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid4_str() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class StringEnum(enum.StrEnum):
    """Enum persisted by its stable string value."""


class CredentialKind(StringEnum):
    GIT = "git"
    SSH = "ssh"
    REGISTRY = "registry"
    WEBHOOK = "webhook"
    SMTP = "smtp"
    NOTIFICATION = "notification"


class NotificationKind(StringEnum):
    DINGTALK = "dingtalk"
    WEBHOOK = "webhook"
    SMTP = "smtp"


class RunStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeploymentStatus(StringEnum):
    PENDING = "pending"
    DEPLOYING = "deploying"
    HEALTHY = "healthy"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ApprovalState(StringEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationKind(StringEnum):
    BUILD = "build"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    SCRIPT = "script"


class TaskKind(StringEnum):
    PIPELINE = "pipeline"
    DEPLOYMENT = "deployment"
    SCRIPT = "script"
    METRICS = "metrics"


class TaskState(StringEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def enum_type(cls: type[StringEnum]) -> Enum:
    return Enum(cls, native_enum=False, values_callable=lambda values: [item.value for item in values])


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Credential(Base, TimestampMixin):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[CredentialKind] = mapped_column(enum_type(CredentialKind), nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class NotificationChannel(Base, TimestampMixin):
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[NotificationKind] = mapped_column(enum_type(NotificationKind), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    encrypted_config: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    target_hint: Mapped[str | None] = mapped_column(String(500))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MCPAccessToken(Base):
    __tablename__ = "mcp_access_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    repo_url: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    git_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    webhook_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    registry_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    dockerfile_source: Mapped[str] = mapped_column(String(20), default="repository")
    dockerfile_path: Mapped[str] = mapped_column(String(500), default="Dockerfile")
    dockerfile_content: Mapped[str | None] = mapped_column(Text)
    build_context: Mapped[str] = mapped_column(String(500), default=".")
    image_repository: Mapped[str | None] = mapped_column(String(1000))
    build_args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pipeline_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    environments: Mapped[list[DeploymentEnvironment]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Server(Base, TimestampMixin):
    __tablename__ = "servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    ssh_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    host_key: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeploymentEnvironment(Base, TimestampMixin):
    __tablename__ = "deployment_environments"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_environment_project_name"),
        UniqueConstraint(
            "server_id",
            "deploy_path",
            name="uq_environment_server_deploy_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    compose_source: Mapped[str] = mapped_column(String(20), default="repository")
    compose_path: Mapped[str] = mapped_column(String(500), default="compose.yaml")
    compose_content: Mapped[str | None] = mapped_column(Text)
    deploy_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    env_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    healthcheck: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[Project] = relationship(back_populates="environments")


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_project_created", "project_id", "created_at"),
        Index("ix_pipeline_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("deployment_environments.id"))
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus), default=RunStatus.QUEUED)
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger_actor: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str | None] = mapped_column(String(30))
    delivery_id: Mapped[str | None] = mapped_column(String(255))
    commit_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    ref: Mapped[str] = mapped_column(String(500), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String(1000))
    image_digest: Mapped[str | None] = mapped_column(String(255))
    current_stage: Mapped[str | None] = mapped_column(String(80))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    leased_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_log_sequence"),
        Index("ix_run_logs_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(20), default="info")
    stage: Mapped[str | None] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deployment(Base, TimestampMixin):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_environment_created", "environment_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    environment_id: Mapped[str] = mapped_column(ForeignKey("deployment_environments.id"))
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    status: Mapped[DeploymentStatus] = mapped_column(
        enum_type(DeploymentStatus), default=DeploymentStatus.PENDING
    )
    image_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_revision: Mapped[str | None] = mapped_column(String(255))
    previous_deployment_id: Mapped[str | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL")
    )
    compose_content: Mapped[str | None] = mapped_column(Text)
    compose_sha256: Mapped[str | None] = mapped_column(String(64))
    environment_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    healthcheck_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class Script(Base, TimestampMixin):
    __tablename__ = "scripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    versions: Mapped[list[ScriptVersion]] = relationship(
        back_populates="script", cascade="all, delete-orphan"
    )


class ScriptVersion(Base):
    __tablename__ = "script_versions"
    __table_args__ = (UniqueConstraint("script_id", "version", name="uq_script_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    script_id: Mapped[str] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    script: Mapped[Script] = relationship(back_populates="versions")


class OperationRequest(Base, TimestampMixin):
    __tablename__ = "operation_requests"
    __table_args__ = (Index("ix_operation_requests_state_created", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    kind: Mapped[OperationKind] = mapped_column(enum_type(OperationKind), nullable=False)
    state: Mapped[ApprovalState] = mapped_column(
        enum_type(ApprovalState), default=ApprovalState.PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RunnerTask(Base, TimestampMixin):
    __tablename__ = "runner_tasks"
    __table_args__ = (
        Index("ix_runner_tasks_claim", "state", "available_at", "priority"),
        Index("ix_runner_tasks_resource", "resource_key", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    kind: Mapped[TaskKind] = mapped_column(enum_type(TaskKind), nullable=False)
    state: Mapped[TaskState] = mapped_column(enum_type(TaskState), default=TaskState.PENDING)
    resource_key: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"))
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operation_requests.id"))
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class HostMetric(Base):
    __tablename__ = "host_metrics"
    __table_args__ = (Index("ix_host_metrics_server_collected", "server_id", "collected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_percent: Mapped[float] = mapped_column(nullable=False)
    memory_total: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_used: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_total: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_used: Mapped[int] = mapped_column(Integer, nullable=False)
    network_rx: Mapped[int] = mapped_column(Integer, nullable=False)
    network_tx: Mapped[int] = mapped_column(Integer, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("provider", "delivery_id", name="uq_webhook_provider_delivery"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(30), default="success", nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(100))
    trace_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
