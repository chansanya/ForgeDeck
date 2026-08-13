"""Add an optional registry credential to projects.

Revision ID: 0003_project_registry
Revises: 0002_notify_mcp_tokens
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_project_registry"
down_revision: str | None = "0002_notify_mcp_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FOREIGN_KEY = "fk_projects_registry_credential_id_credentials"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "registry_credential_id" in columns:
        return
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("registry_credential_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            _FOREIGN_KEY,
            "credentials",
            ["registry_credential_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "registry_credential_id" not in columns:
        return
    foreign_keys = sa.inspect(bind).get_foreign_keys("projects")
    with op.batch_alter_table("projects") as batch_op:
        if any(key.get("name") == _FOREIGN_KEY for key in foreign_keys):
            batch_op.drop_constraint(_FOREIGN_KEY, type_="foreignkey")
        batch_op.drop_column("registry_credential_id")
