"""Replace shared residential tiers with one dedicated IP per subscription.

Revision ID: 0017_dedicated_egress
Revises: 0016_fixed_30_day_cycles
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_dedicated_egress"
down_revision: str | None = "0016_fixed_30_day_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_egress_tier_capacity", "egress_endpoints", type_="check")
    op.drop_column("egress_endpoints", "tier")
    op.drop_column("plans", "egress_tier")
    op.drop_column("plans", "device_limit")

    op.create_check_constraint(
        "ck_egress_capacity_one", "egress_endpoints", "capacity = 1"
    )
    op.create_check_constraint(
        "ck_egress_current_count_range",
        "egress_endpoints",
        "current_count BETWEEN 0 AND 1",
    )
    op.execute(
        "UPDATE egress_endpoints SET status = 'AVAILABLE' "
        "WHERE status IN ('HEALTHY', 'ACTIVE') AND current_count = 0"
    )
    op.execute(
        "UPDATE egress_endpoints SET status = 'ASSIGNED' "
        "WHERE status IN ('HEALTHY', 'ACTIVE') AND current_count = 1"
    )
    op.execute(
        "UPDATE egress_endpoints SET status = 'DEGRADED' "
        "WHERE status NOT IN ('AVAILABLE', 'ASSIGNED', 'QUARANTINED', 'DEGRADED', 'RETIRED')"
    )
    op.create_check_constraint(
        "ck_egress_status", "egress_endpoints",
        "status IN ('AVAILABLE', 'ASSIGNED', 'QUARANTINED', 'DEGRADED', 'RETIRED')",
    )
    op.add_column(
        "egress_bindings",
        sa.Column(
            "active_egress_id",
            sa.Integer(),
            sa.Computed("IF(released_at IS NULL, egress_id, NULL)", persisted=True),
        ),
    )
    op.create_unique_constraint(
        "uq_egress_active_egress", "egress_bindings", ["active_egress_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_egress_active_egress", "egress_bindings", type_="unique")
    op.drop_column("egress_bindings", "active_egress_id")
    op.drop_constraint("ck_egress_status", "egress_endpoints", type_="check")
    op.drop_constraint("ck_egress_current_count_range", "egress_endpoints", type_="check")
    op.drop_constraint("ck_egress_capacity_one", "egress_endpoints", type_="check")
    op.add_column("plans", sa.Column("device_limit", sa.Integer(), nullable=False, server_default="5"))
    op.add_column("plans", sa.Column("egress_tier", sa.String(16), nullable=False, server_default="DEDICATED"))
    op.add_column("egress_endpoints", sa.Column("tier", sa.String(16), nullable=False, server_default="DEDICATED"))
    op.create_check_constraint(
        "ck_egress_tier_capacity", "egress_endpoints",
        "(tier = 'STANDARD' AND capacity = 5) OR (tier = 'AI' AND capacity = 2) OR (tier = 'DEDICATED' AND capacity = 1)",
    )
