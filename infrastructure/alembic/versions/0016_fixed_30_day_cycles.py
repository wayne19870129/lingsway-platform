"""Enforce fixed 30-day rolling billing cycles.

Revision ID: 0016_fixed_30_day_cycles
Revises: 0015_security
"""

from typing import Sequence

from alembic import op

revision: str = "0016_fixed_30_day_cycles"
down_revision: str | None = "0015_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_plan_fixed_30_day_duration", "plans", "duration_days = 30"
    )


def downgrade() -> None:
    op.drop_constraint("ck_plan_fixed_30_day_duration", "plans", type_="check")
