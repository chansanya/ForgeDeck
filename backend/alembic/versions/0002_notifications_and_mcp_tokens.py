"""Add notification channels and scoped MCP access tokens.

Revision ID: 0002_notify_mcp_tokens
Revises: 0001_initial
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_notify_mcp_tokens"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("notification_channels"):
        op.create_table(
            "notification_channels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "dingtalk",
                "webhook",
                "smtp",
                name="notificationkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("encrypted_config", sa.LargeBinary(), nullable=False),
        sa.Column("target_hint", sa.String(length=500), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
    if not inspector.has_table("mcp_access_tokens"):
        op.create_table(
            "mcp_access_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_mcp_access_tokens_token_hash",
            "mcp_access_tokens",
            ["token_hash"],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("mcp_access_tokens"):
        op.drop_table("mcp_access_tokens")
    if inspector.has_table("notification_channels"):
        op.drop_table("notification_channels")
