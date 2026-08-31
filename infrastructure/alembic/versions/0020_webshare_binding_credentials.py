"""Add Webshare sub-user records and binding-level credentials.

Revision ID: 0020_webshare_creds
Revises: 0019_gateway_routes
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0020_webshare_creds"
down_revision: str | None = "0019_gateway_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "egress_bindings",
        sa.Column("credential_secret_ref", sa.String(length=200), nullable=True),
    )

    op.create_table(
        "webshare_subusers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("webshare_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("proxy_limit_gb", sa.Numeric(12, 3), nullable=False),
        sa.Column("max_thread_count", sa.Integer(), nullable=False),
        sa.Column("credential_secret_ref", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.UniqueConstraint("subscription_id", name="uq_webshare_subuser_subscription"),
        sa.UniqueConstraint("webshare_id", name="uq_webshare_subuser_webshare_id"),
        sa.UniqueConstraint("label", name="uq_webshare_subuser_label"),
        sa.UniqueConstraint(
            "credential_secret_ref", name="uq_webshare_subuser_secret_ref"
        ),
    )
    op.create_index(
        "ix_webshare_subusers_subscription_id",
        "webshare_subusers",
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_webshare_subusers_subscription_id", table_name="webshare_subusers")
    op.drop_table("webshare_subusers")
    op.drop_column("egress_bindings", "credential_secret_ref")
