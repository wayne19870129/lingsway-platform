"""JWT sessions and durable rate-limit buckets.

Revision ID: 0015_security
Revises: 0014_payment_provider
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0015_security"
down_revision: str | None = "0014_payment_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRECISE_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "jwt_sessions",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("issued_at", PRECISE_DATETIME, nullable=False),
        sa.Column("expires_at", PRECISE_DATETIME, nullable=False),
        sa.Column("revoked_at", PRECISE_DATETIME),
        sa.Column("revoke_reason", sa.String(40)),
    )
    op.create_index("ix_jwt_sessions_customer_id", "jwt_sessions", ["customer_id"])
    op.create_index("ix_jwt_sessions_expires_at", "jwt_sessions", ["expires_at"])
    op.create_table(
        "rate_limit_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(40), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("window_start", PRECISE_DATETIME, nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", PRECISE_DATETIME, nullable=False),
        sa.UniqueConstraint(
            "scope", "key_hash", "window_start", name="uq_rate_limit_bucket"
        ),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
    op.drop_table("jwt_sessions")
