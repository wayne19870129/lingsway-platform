"""Enforce microsecond precision on authoritative accounting timestamps.

Revision ID: 0010_datetime_fsp6
Revises: 0009_usage_ledger
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0010_datetime_fsp6"
down_revision: str | None = "0009_usage_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _alter(type_: sa.types.TypeEngine) -> None:
    op.alter_column(
        "subscriptions",
        "service_expire_at",
        existing_type=sa.DateTime(timezone=True),
        type_=type_,
        existing_nullable=False,
    )
    op.alter_column(
        "subscriptions",
        "last_usage_synced_at",
        existing_type=sa.DateTime(timezone=True),
        type_=type_,
        existing_nullable=True,
    )
    op.alter_column(
        "usage_samples",
        "sampled_at",
        existing_type=sa.DateTime(timezone=True),
        type_=type_,
        existing_nullable=False,
    )
    for column in ("period_start", "period_end", "created_at"):
        op.alter_column(
            "usage_periods",
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=type_,
            existing_nullable=False,
        )


def upgrade() -> None:
    _alter(mysql.DATETIME(fsp=6))


def downgrade() -> None:
    _alter(mysql.DATETIME())
