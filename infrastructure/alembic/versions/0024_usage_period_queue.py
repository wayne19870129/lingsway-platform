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
    def refresh() -> sa.Inspector:
        return sa.inspect(bind)

    inspector = refresh()
    columns = {column["name"]: column for column in inspector.get_columns("usage_periods")}
    if "order_id" not in columns:
        op.add_column("usage_periods", sa.Column("order_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE usage_periods up JOIN subscriptions s "
        "ON s.id = up.subscription_id SET up.order_id = s.order_id "
        "WHERE up.order_id IS NULL"
    )
    if columns.get("order_id", {}).get("nullable", True):
        op.alter_column("usage_periods", "order_id", nullable=False)
    inspector = refresh()
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("usage_periods")}
    if "fk_usage_periods_order_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_usage_periods_order_id", "usage_periods", "orders", ["order_id"], ["id"]
        )
    inspector = refresh()
    unique_names = {constraint["name"] for constraint in inspector.get_unique_constraints("usage_periods")}
    if "uq_usage_period_order_id" not in unique_names:
        op.create_unique_constraint("uq_usage_period_order_id", "usage_periods", ["order_id"])
    inspector = refresh()
    columns = {column["name"] for column in inspector.get_columns("usage_periods")}
    if "plan_id" not in columns:
        op.add_column("usage_periods", sa.Column("plan_id", sa.Integer(), nullable=True))
    inspector = refresh()
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("usage_periods")}
    if "fk_usage_periods_plan_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_usage_periods_plan_id", "usage_periods", "plans", ["plan_id"], ["id"]
        )
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE usage_periods MODIFY status "
            "ENUM('ACTIVE','QUEUED','CLOSED') NOT NULL"
        )
        op.execute(
            "ALTER TABLE subscriptions MODIFY reconcile_state "
            "ENUM('SYNCED','PENDING','PENDING_MANUAL','FAILED') NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("usage_periods")}
    queued = bind.execute(sa.text("SELECT COUNT(*) FROM usage_periods WHERE status = 'QUEUED'"))
    if queued.scalar_one() > 0:
        raise RuntimeError("Cannot downgrade while QUEUED usage periods exist")
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE usage_periods MODIFY status "
            "ENUM('ACTIVE','CLOSED') NOT NULL"
        )
        op.execute(
            "ALTER TABLE subscriptions MODIFY reconcile_state "
            "ENUM('SYNCED','PENDING','FAILED') NOT NULL"
        )
    if "plan_id" in columns:
        op.drop_constraint("fk_usage_periods_plan_id", "usage_periods", type_="foreignkey")
        op.drop_column("usage_periods", "plan_id")
    if "order_id" in columns:
        op.drop_constraint("uq_usage_period_order_id", "usage_periods", type_="unique")
        op.drop_constraint("fk_usage_periods_order_id", "usage_periods", type_="foreignkey")
        op.drop_column("usage_periods", "order_id")
