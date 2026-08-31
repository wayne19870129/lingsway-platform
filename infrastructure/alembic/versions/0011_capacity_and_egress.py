"""Independent transport-byte and residential-seat capacity models.

Revision ID: 0011_capacity_egress
Revises: 0010_datetime_fsp6
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0011_capacity_egress"
down_revision: str | None = "0010_datetime_fsp6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("egress_tier", sa.String(16), nullable=False, server_default="STANDARD"),
    )
    op.alter_column(
        "transport_providers",
        "purchased_bytes",
        new_column_name="quota_bytes",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.add_column(
        "transport_providers",
        sa.Column("measured_at", mysql.DATETIME(fsp=6), nullable=True),
    )

    op.alter_column(
        "egress_endpoints",
        "target_capacity_users",
        new_column_name="capacity",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "egress_endpoints",
        "assigned_users",
        new_column_name="current_count",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.add_column(
        "egress_endpoints",
        sa.Column("tier", sa.String(16), nullable=False, server_default="STANDARD"),
    )
    op.add_column(
        "egress_endpoints", sa.Column("mihomo_listen_port", sa.Integer(), nullable=True)
    )
    op.execute("UPDATE egress_endpoints SET capacity = 5, mihomo_listen_port = 11000 + id")
    op.alter_column(
        "egress_endpoints", "mihomo_listen_port", existing_type=sa.Integer(), nullable=False
    )
    op.create_unique_constraint(
        "uq_egress_mihomo_listen_port", "egress_endpoints", ["mihomo_listen_port"]
    )
    op.create_check_constraint(
        "ck_egress_tier_capacity",
        "egress_endpoints",
        "(tier = 'STANDARD' AND capacity = 5) OR "
        "(tier = 'AI' AND capacity = 2) OR "
        "(tier = 'DEDICATED' AND capacity = 1)",
    )

    op.create_table(
        "egress_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("egress_id", sa.Integer(), sa.ForeignKey("egress_endpoints.id"), nullable=False),
        sa.Column("assigned_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("released_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "active_subscription_id",
            sa.Integer(),
            sa.Computed("IF(released_at IS NULL, subscription_id, NULL)", persisted=True),
        ),
        sa.UniqueConstraint("active_subscription_id", name="uq_egress_active_subscription"),
    )
    op.create_index("ix_egress_bindings_subscription_id", "egress_bindings", ["subscription_id"])
    op.create_index("ix_egress_bindings_egress_id", "egress_bindings", ["egress_id"])

    op.create_table(
        "transport_capacity_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "provider_id", sa.Integer(), sa.ForeignKey("transport_providers.id"), nullable=False
        ),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "provider_id", "threshold", "quota_bytes", name="uq_transport_capacity_alert"
        ),
    )
    op.create_index(
        "ix_transport_capacity_alerts_provider_id",
        "transport_capacity_alerts",
        ["provider_id"],
    )


def downgrade() -> None:
    op.drop_table("transport_capacity_alerts")
    op.drop_table("egress_bindings")
    op.drop_constraint("ck_egress_tier_capacity", "egress_endpoints", type_="check")
    op.drop_constraint(
        "uq_egress_mihomo_listen_port", "egress_endpoints", type_="unique"
    )
    op.drop_column("egress_endpoints", "mihomo_listen_port")
    op.drop_column("egress_endpoints", "tier")
    op.alter_column(
        "egress_endpoints",
        "current_count",
        new_column_name="assigned_users",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "egress_endpoints",
        "capacity",
        new_column_name="target_capacity_users",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_column("transport_providers", "measured_at")
    op.alter_column(
        "transport_providers",
        "quota_bytes",
        new_column_name="purchased_bytes",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.drop_column("plans", "egress_tier")
