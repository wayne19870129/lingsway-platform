"""Add queued usage-period state and retain the selected plan per period.

Revision ID: 0024_usage_period_queue
Revises: 0023_secret_revision
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_usage_period_queue"
down_revision: str | None = "0023_secret_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("usage_periods")}
    if "plan_id" not in columns:
        op.add_column("usage_periods", sa.Column("plan_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_usage_periods_plan_id", "usage_periods", "plans", ["plan_id"], ["id"]
        )
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE usage_periods MODIFY status "
            "ENUM('ACTIVE','QUEUED','CLOSED') NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("usage_periods")}
    if "plan_id" in columns:
        op.drop_constraint("fk_usage_periods_plan_id", "usage_periods", type_="foreignkey")
        op.drop_column("usage_periods", "plan_id")
