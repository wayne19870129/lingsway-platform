"""Phase 2 accounting sync and jobs.

Revision ID: 0002_accounting
Revises: 0001_phase1
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_accounting"
down_revision: str | None = "0001_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("last_usage_synced_at", sa.DateTime(timezone=True)))
    op.add_column("subscriptions", sa.Column("quota_enforced_at", sa.DateTime(timezone=True)))
    op.add_column("subscriptions", sa.Column("expire_enforced_at", sa.DateTime(timezone=True)))
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
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", name="jobstatus"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("usage_records")
    op.drop_column("subscriptions", "expire_enforced_at")
    op.drop_column("subscriptions", "quota_enforced_at")
    op.drop_column("subscriptions", "last_usage_synced_at")
