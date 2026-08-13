"""Initial control-plane schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("source_ip", sa.String(length=100), nullable=True),
        sa.Column("trace_id", sa.String(length=100), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "git",
                "ssh",
                "registry",
                "webhook",
                "smtp",
                "notification",
                name="credentialkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "operation_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "build",
                "deploy",
                "rollback",
                "script",
                name="operationkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "expired",
                "executing",
                "succeeded",
                "failed",
                name="approvalstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameter_hash", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operation_requests_state_created",
        "operation_requests",
        ["state", "created_at"],
    )
    op.create_table(
        "scripts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("repo_url", sa.String(length=1000), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("git_credential_id", sa.String(length=36), nullable=True),
        sa.Column("webhook_credential_id", sa.String(length=36), nullable=True),
        sa.Column("dockerfile_source", sa.String(length=20), nullable=False),
        sa.Column("dockerfile_path", sa.String(length=500), nullable=False),
        sa.Column("dockerfile_content", sa.Text(), nullable=True),
        sa.Column("build_context", sa.String(length=500), nullable=False),
        sa.Column("image_repository", sa.String(length=1000), nullable=True),
        sa.Column("build_args", sa.JSON(), nullable=False),
        sa.Column("pipeline_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["git_credential_id"], ["credentials.id"]),
        sa.ForeignKeyConstraint(["webhook_credential_id"], ["credentials.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_projects_repo_url", "projects", ["repo_url"])
    op.create_table(
        "script_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("script_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id", "version", name="uq_script_version"),
    )
    op.create_table(
        "servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("ssh_credential_id", sa.String(length=36), nullable=True),
        sa.Column("host_key", sa.Text(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ssh_credential_id"], ["credentials.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "deployment_environments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("compose_source", sa.String(length=20), nullable=False),
        sa.Column("compose_path", sa.String(length=500), nullable=False),
        sa.Column("compose_content", sa.Text(), nullable=True),
        sa.Column("deploy_path", sa.String(length=1000), nullable=False),
        sa.Column("env_config", sa.JSON(), nullable=False),
        sa.Column("healthcheck", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_environment_project_name"),
    )
    op.create_table(
        "host_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("memory_total", sa.Integer(), nullable=False),
        sa.Column("memory_used", sa.Integer(), nullable=False),
        sa.Column("disk_total", sa.Integer(), nullable=False),
        sa.Column("disk_used", sa.Integer(), nullable=False),
        sa.Column("network_rx", sa.Integer(), nullable=False),
        sa.Column("network_tx", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_host_metrics_server_collected",
        "host_metrics",
        ["server_id", "collected_at"],
    )
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("environment_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="runstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(length=30), nullable=False),
        sa.Column("trigger_actor", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=30), nullable=True),
        sa.Column("delivery_id", sa.String(length=255), nullable=True),
        sa.Column("commit_sha", sa.String(length=128), nullable=False),
        sa.Column("ref", sa.String(length=500), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("image_ref", sa.String(length=1000), nullable=True),
        sa.Column("image_digest", sa.String(length=255), nullable=True),
        sa.Column("current_stage", sa.String(length=80), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("leased_by", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["deployment_environments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pipeline_runs_project_created",
        "pipeline_runs",
        ["project_id", "created_at"],
    )
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("environment_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "deploying",
                "healthy",
                "failed",
                "rolled_back",
                name="deploymentstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("image_ref", sa.String(length=1000), nullable=False),
        sa.Column("image_digest", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.String(length=255), nullable=False),
        sa.Column("previous_revision", sa.String(length=255), nullable=True),
        sa.Column("compose_content", sa.Text(), nullable=True),
        sa.Column("compose_sha256", sa.String(length=64), nullable=True),
        sa.Column("environment_snapshot", sa.JSON(), nullable=False),
        sa.Column("healthcheck_result", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["deployment_environments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deployments_environment_created",
        "deployments",
        ["environment_id", "created_at"],
    )
    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_log_sequence"),
    )
    op.create_index(
        "ix_run_logs_run_sequence", "run_logs", ["run_id", "sequence"]
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "delivery_id", name="uq_webhook_provider_delivery"
        ),
    )
    op.create_table(
        "runner_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "pipeline",
                "deployment",
                "script",
                "metrics",
                name="taskkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "leased",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="taskstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("resource_key", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_by", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["operation_requests.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runner_tasks_claim",
        "runner_tasks",
        ["state", "available_at", "priority"],
    )
    op.create_index(
        "ix_runner_tasks_resource",
        "runner_tasks",
        ["resource_key", "state"],
    )


def downgrade() -> None:
    for table_name in (
        "runner_tasks",
        "webhook_deliveries",
        "run_logs",
        "deployments",
        "pipeline_runs",
        "host_metrics",
        "deployment_environments",
        "servers",
        "script_versions",
        "projects",
        "scripts",
        "operation_requests",
        "credentials",
        "audit_events",
        "admin_users",
    ):
        op.drop_table(table_name)
