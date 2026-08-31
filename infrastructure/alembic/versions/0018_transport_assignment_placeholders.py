"""Reserve Transport node assignment and health tables.

Revision ID: 0018_transport_assign
Revises: 0017_dedicated_egress
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0018_transport_assign"
down_revision: str | None = "0017_dedicated_egress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "egress_transport_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("egress_id", sa.Integer(), nullable=False),
        sa.Column("transport_provider_id", sa.Integer(), nullable=False),
        sa.Column("transport_node_id", sa.Integer(), nullable=False),
        sa.Column("allocation_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.Enum("ACTIVE", "DRAINING", "RELEASED", name="egresstransportassignmentstate"), nullable=False, server_default="ACTIVE"),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("last_latency_ms", sa.Integer()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("unavailable_since", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["egress_id"], ["egress_endpoints.id"]),
        sa.ForeignKeyConstraint(["transport_provider_id"], ["transport_providers.id"]),
        sa.ForeignKeyConstraint(["transport_node_id"], ["transport_endpoints.id"]),
        sa.UniqueConstraint("egress_id", "transport_node_id", "allocation_generation", name="uq_egress_transport_assignment_generation"),
    )
    op.create_index("ix_egress_transport_assignments_egress_id", "egress_transport_assignments", ["egress_id"])
    op.create_index("ix_egress_transport_assignments_transport_provider_id", "egress_transport_assignments", ["transport_provider_id"])
    op.create_index("ix_egress_transport_assignments_transport_node_id", "egress_transport_assignments", ["transport_node_id"])

    op.create_table(
        "transport_node_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transport_provider_id", sa.Integer(), nullable=False),
        sa.Column("transport_node_id", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("packet_loss", sa.Integer()),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_reason", sa.String(length=255)),
        sa.ForeignKeyConstraint(["transport_provider_id"], ["transport_providers.id"]),
        sa.ForeignKeyConstraint(["transport_node_id"], ["transport_endpoints.id"]),
    )
    op.create_index("ix_transport_node_health_transport_provider_id", "transport_node_health", ["transport_provider_id"])
    op.create_index("ix_transport_node_health_transport_node_id", "transport_node_health", ["transport_node_id"])
    op.create_index("ix_transport_node_health_measured_at", "transport_node_health", ["measured_at"])


def downgrade() -> None:
    op.drop_index("ix_transport_node_health_measured_at", table_name="transport_node_health")
    op.drop_index("ix_transport_node_health_transport_node_id", table_name="transport_node_health")
    op.drop_index("ix_transport_node_health_transport_provider_id", table_name="transport_node_health")
    op.drop_table("transport_node_health")
    op.drop_index("ix_egress_transport_assignments_transport_node_id", table_name="egress_transport_assignments")
    op.drop_index("ix_egress_transport_assignments_transport_provider_id", table_name="egress_transport_assignments")
    op.drop_index("ix_egress_transport_assignments_egress_id", table_name="egress_transport_assignments")
    op.drop_table("egress_transport_assignments")
