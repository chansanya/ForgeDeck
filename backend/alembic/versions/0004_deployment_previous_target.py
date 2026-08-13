"""Bind deployments to an exact previous deployment.

Revision ID: 0004_deployment_previous
Revises: 0003_project_registry
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_deployment_previous"
down_revision: str | None = "0003_project_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FOREIGN_KEY = "fk_deployments_previous_deployment_id_deployments"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("deployments")}
    if "previous_deployment_id" in columns:
        return
    with op.batch_alter_table("deployments") as batch_op:
        batch_op.add_column(sa.Column("previous_deployment_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            _FOREIGN_KEY,
            "deployments",
            ["previous_deployment_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("deployments")}
    if "previous_deployment_id" not in columns:
        return
    foreign_keys = sa.inspect(bind).get_foreign_keys("deployments")
    with op.batch_alter_table("deployments") as batch_op:
        if any(key.get("name") == _FOREIGN_KEY for key in foreign_keys):
            batch_op.drop_constraint(_FOREIGN_KEY, type_="foreignkey")
        batch_op.drop_column("previous_deployment_id")
