"""Phase 7 operations, content, and abuse watch.

Revision ID: 0007_operations
Revises: 0006_billing
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_operations"
down_revision: str | None = "0006_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("transport_providers.id"), nullable=False),
        sa.Column("purchased_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("remaining_bytes", sa.BigInteger(), nullable=False),
        sa.Column("cycle_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_usage_provider_id", "provider_usage", ["provider_id"])
    op.create_table(
        "gateway_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gateway_code", sa.String(64), nullable=False),
        sa.Column("cpu_percent", sa.Integer(), nullable=False),
        sa.Column("ram_percent", sa.Integer(), nullable=False),
        sa.Column("disk_percent", sa.Integer(), nullable=False),
        sa.Column("network_in_bps", sa.BigInteger(), nullable=False),
        sa.Column("network_out_bps", sa.BigInteger(), nullable=False),
        sa.Column("active_connections", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gateway_metrics_gateway_code", "gateway_metrics", ["gateway_code"])
    op.create_index("ix_gateway_metrics_collected_at", "gateway_metrics", ["collected_at"])
    op.create_table(
        "abuse_watch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("signal", sa.String(50), nullable=False),
        sa.Column("observed_value", sa.BigInteger(), nullable=False),
        sa.Column("baseline_value", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_abuse_watch_subscription_id", "abuse_watch", ["subscription_id"])
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tickets_customer_id", "tickets", ["customer_id"])
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tutorials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("client_name", sa.String(80), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tutorials_platform", "tutorials", ["platform"])


def downgrade() -> None:
    op.drop_table("tutorials")
    op.drop_table("announcements")
    op.drop_table("tickets")
    op.drop_table("abuse_watch")
    op.drop_table("gateway_metrics")
    op.drop_table("provider_usage")
