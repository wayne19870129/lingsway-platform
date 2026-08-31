"""Add fail-closed per-user gateway route bindings.

Revision ID: 0019_gateway_route_bindings
Revises: 0018_transport_assign
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019_gateway_routes"
down_revision: str | None = "0018_transport_assign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_route_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("egress_id", sa.Integer(), nullable=False),
        sa.Column("gateway_principal", sa.String(length=128), nullable=False),
        sa.Column("outbound_tag", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("active_subscription_id", sa.Integer(), sa.Computed("IF(released_at IS NULL AND enabled = 1, subscription_id, NULL)", persisted=True)),
        sa.Column("active_egress_id", sa.Integer(), sa.Computed("IF(released_at IS NULL AND enabled = 1, egress_id, NULL)", persisted=True)),
        sa.Column("active_gateway_principal", sa.String(length=128), sa.Computed("IF(released_at IS NULL AND enabled = 1, gateway_principal, NULL)", persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.ForeignKeyConstraint(["egress_id"], ["egress_endpoints.id"]),
        sa.UniqueConstraint("outbound_tag", name="uq_gateway_route_outbound_tag"),
        sa.UniqueConstraint("active_subscription_id", name="uq_gateway_route_active_subscription"),
        sa.UniqueConstraint("active_egress_id", name="uq_gateway_route_active_egress"),
        sa.UniqueConstraint("active_gateway_principal", name="uq_gateway_route_active_principal"),
    )
    op.create_index("ix_gateway_route_bindings_subscription_id", "gateway_route_bindings", ["subscription_id"])
    op.create_index("ix_gateway_route_bindings_egress_id", "gateway_route_bindings", ["egress_id"])
    op.create_index("ix_gateway_route_bindings_gateway_principal", "gateway_route_bindings", ["gateway_principal"])


def downgrade() -> None:
    op.drop_index("ix_gateway_route_bindings_gateway_principal", table_name="gateway_route_bindings")
    op.drop_index("ix_gateway_route_bindings_egress_id", table_name="gateway_route_bindings")
    op.drop_index("ix_gateway_route_bindings_subscription_id", table_name="gateway_route_bindings")
    op.drop_table("gateway_route_bindings")
