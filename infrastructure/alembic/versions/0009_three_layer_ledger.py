"""Three-layer usage ledger and database-truth reconcile state.

Revision ID: 0009_usage_ledger
Revises: 0008_transport_ext
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0009_usage_ledger"
down_revision: str | None = "0008_transport_ext"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("usage_alerts")
    op.drop_table("usage_records")

    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("expire_enforced_at")
        batch.drop_column("quota_enforced_at")
        batch.drop_column("current_period_end")
        batch.drop_column("current_period_start")
        batch.drop_column("addon_traffic_bytes")
        batch.drop_column("traffic_used_bytes")
        batch.drop_column("traffic_quota_bytes")
        batch.add_column(
            sa.Column(
                "reconcile_state",
                sa.Enum("SYNCED", "PENDING", "FAILED", name="reconcilestate"),
                nullable=False,
                server_default="PENDING",
            )
        )
        batch.add_column(
            sa.Column("reconcile_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.alter_column(
            "service_expire_at",
            existing_type=sa.DateTime(timezone=True),
            type_=mysql.DATETIME(fsp=6),
            existing_nullable=False,
        )
        batch.alter_column(
            "last_usage_synced_at",
            existing_type=sa.DateTime(timezone=True),
            type_=mysql.DATETIME(fsp=6),
            existing_nullable=True,
        )
    op.create_index("ix_subscriptions_reconcile_state", "subscriptions", ["reconcile_state"])

    op.create_table(
        "usage_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("source_counter", sa.BigInteger(), nullable=False),
        sa.Column("sampled_at", mysql.DATETIME(fsp=6), nullable=False),
    )
    op.create_index("ix_usage_samples_subscription_id", "usage_samples", ["subscription_id"])
    op.create_index("ix_usage_samples_sampled_at", "usage_samples", ["sampled_at"])
    op.create_index(
        "ix_usage_samples_subscription_node_sampled",
        "usage_samples",
        ["subscription_id", "node_id", "sampled_at"],
    )

    op.create_table(
        "usage_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("period_start", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("period_end", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CLOSED", name="usageperiodstatus"),
            nullable=False,
        ),
        sa.Column(
            "active_subscription_id",
            sa.Integer(),
            sa.Computed(
                "CASE WHEN status = 'ACTIVE' THEN subscription_id ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint("active_subscription_id", name="uq_usage_period_active_subscription"),
    )
    op.create_index("ix_usage_periods_subscription_id", "usage_periods", ["subscription_id"])

    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("current_period_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_subscriptions_current_period", "usage_periods", ["current_period_id"], ["id"]
        )

    op.create_table(
        "usage_deltas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("period_id", sa.Integer(), sa.ForeignKey("usage_periods.id"), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("from_sample_id", sa.BigInteger(), sa.ForeignKey("usage_samples.id")),
        sa.Column("to_sample_id", sa.BigInteger(), sa.ForeignKey("usage_samples.id"), nullable=False),
        sa.Column("delta_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum("normal", "counter_reset", "bootstrap", name="usagedeltareason"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "subscription_id", "node_id", "to_sample_id", name="uq_usage_delta_sample"
        ),
    )
    op.create_index("ix_usage_deltas_subscription_id", "usage_deltas", ["subscription_id"])
    op.create_index("ix_usage_deltas_period_id", "usage_deltas", ["period_id"])

    op.create_table(
        "usage_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("period_id", sa.Integer(), sa.ForeignKey("usage_periods.id"), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("period_id", "threshold", name="uq_usage_alert_period"),
    )
    op.create_index("ix_usage_alerts_subscription_id", "usage_alerts", ["subscription_id"])
    op.create_index("ix_usage_alerts_period_id", "usage_alerts", ["period_id"])


def downgrade() -> None:
    op.drop_table("usage_alerts")
    op.drop_table("usage_deltas")
    op.drop_index("ix_subscriptions_reconcile_state", table_name="subscriptions")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("reconcile_attempts")
        batch.drop_column("reconcile_state")
        batch.add_column(sa.Column("traffic_quota_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("traffic_used_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("addon_traffic_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("quota_enforced_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("expire_enforced_at", sa.DateTime(timezone=True)))
        batch.alter_column(
            "service_expire_at",
            existing_type=mysql.DATETIME(fsp=6),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
        batch.alter_column(
            "last_usage_synced_at",
            existing_type=mysql.DATETIME(fsp=6),
            type_=sa.DateTime(timezone=True),
            existing_nullable=True,
        )

    op.execute(
        """
        UPDATE subscriptions AS s
        LEFT JOIN usage_periods AS p ON p.id = s.current_period_id
        SET s.traffic_quota_bytes = COALESCE(p.quota_bytes, 0),
            s.traffic_used_bytes = COALESCE(p.used_bytes, 0),
            s.current_period_start = COALESCE(p.period_start, s.created_at),
            s.current_period_end = COALESCE(p.period_end, s.service_expire_at)
        """
    )
    with op.batch_alter_table("subscriptions") as batch:
        batch.alter_column(
            "traffic_quota_bytes", existing_type=sa.BigInteger(), nullable=False
        )
        batch.alter_column(
            "current_period_start", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.alter_column(
            "current_period_end", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.drop_constraint("fk_subscriptions_current_period", type_="foreignkey")
        batch.drop_column("current_period_id")
    op.drop_table("usage_periods")
    op.drop_table("usage_samples")

    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("bytes_up", sa.BigInteger(), nullable=False),
        sa.Column("bytes_down", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_records_subscription_id", "usage_records", ["subscription_id"])
    op.create_table(
        "usage_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "subscription_id", "threshold", "period_start", name="uq_usage_alert_period"
        ),
    )
    op.create_index("ix_usage_alerts_subscription_id", "usage_alerts", ["subscription_id"])
