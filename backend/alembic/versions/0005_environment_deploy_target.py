"""Prevent deployment environments from sharing a server directory.

The uniqueness constraint intentionally rejects ambiguous legacy targets so two
environments cannot concurrently manage the same remote Compose directory.

Revision ID: 0005_environment_target
Revises: 0004_deployment_previous
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_environment_target"
down_revision: str | None = "0004_deployment_previous"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_environment_server_deploy_path"


def upgrade() -> None:
    bind = op.get_bind()
    conflicts = bind.execute(
        sa.text(
            "SELECT server_id, deploy_path, COUNT(*) AS target_count "
            "FROM deployment_environments "
            "GROUP BY server_id, deploy_path "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if conflicts:
        rendered = ", ".join(
            f"server={row.server_id!r} path={row.deploy_path!r} count={row.target_count}"
            for row in conflicts
        )
        raise RuntimeError(
            "cannot add deployment target uniqueness; resolve duplicate environments first: "
            + rendered
        )
    with op.batch_alter_table("deployment_environments") as batch_op:
        batch_op.create_unique_constraint(
            _CONSTRAINT,
            ["server_id", "deploy_path"],
        )


def downgrade() -> None:
    with op.batch_alter_table("deployment_environments") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
