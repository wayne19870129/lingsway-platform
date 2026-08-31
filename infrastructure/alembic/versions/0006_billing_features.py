"""Phase 6 billing changes and usage alerts.

Revision ID: 0006_billing
Revises: 0005_subscription
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_billing"
down_revision: str | None = "0005_subscription"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("target_subscription_id", sa.Integer()))
        batch.add_column(sa.Column("addon_bytes", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_orders_target_subscription", "subscriptions", ["target_subscription_id"], ["id"]
        )
    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("next_plan_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_subscriptions_next_plan", "plans", ["next_plan_id"], ["id"]
        )
    op.create_table(
        "subscription_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("from_plan_id", sa.Integer(), sa.ForeignKey("plans.id")),
        sa.Column("to_plan_id", sa.Integer(), sa.ForeignKey("plans.id")),
        sa.Column("credit_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_due", sa.Numeric(12, 2), nullable=False),
        sa.Column("traffic_before", sa.BigInteger(), nullable=False),
        sa.Column("traffic_after", sa.BigInteger(), nullable=False),
        sa.Column("expire_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expire_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscription_changes_subscription_id", "subscription_changes", ["subscription_id"])
    op.create_table(
        "usage_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subscription_id", "threshold", "period_start", name="uq_usage_alert_period"),
    )
    op.create_index("ix_usage_alerts_subscription_id", "usage_alerts", ["subscription_id"])


def downgrade() -> None:
    op.drop_table("usage_alerts")
    op.drop_table("subscription_changes")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_constraint("fk_subscriptions_next_plan", type_="foreignkey")
        batch.drop_column("next_plan_id")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_target_subscription", type_="foreignkey")
        batch.drop_column("addon_bytes")
        batch.drop_column("target_subscription_id")
