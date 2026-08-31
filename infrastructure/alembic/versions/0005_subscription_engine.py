"""Phase 5 subscription tokens and versions.

Revision ID: 0005_subscription
Revises: 0004_routing
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_subscription"
down_revision: str | None = "0004_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("token_hash", sa.String(64)))
    op.add_column("subscriptions", sa.Column("token_created_at", sa.DateTime(timezone=True)))
    op.add_column("subscriptions", sa.Column("token_revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_subscriptions_token_hash", "subscriptions", ["token_hash"], unique=True)
    op.create_table(
        "subscription_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("previous_version_id", sa.Integer(), sa.ForeignKey("subscription_versions.id")),
        sa.UniqueConstraint("subscription_id", "version_no", name="uq_subscription_version"),
    )
    op.create_index("ix_subscription_versions_subscription_id", "subscription_versions", ["subscription_id"])


def downgrade() -> None:
    op.drop_table("subscription_versions")
    op.drop_index("ix_subscriptions_token_hash", table_name="subscriptions")
    op.drop_column("subscriptions", "token_revoked_at")
    op.drop_column("subscriptions", "token_created_at")
    op.drop_column("subscriptions", "token_hash")
