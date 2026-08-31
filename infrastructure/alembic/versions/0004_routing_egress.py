"""Phase 4 routing, egress, config versions.

Revision ID: 0004_routing
Revises: 0003_transport
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_routing"
down_revision: str | None = "0003_transport"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    role_enum = sa.Enum("PRIMARY", "BACKUP", "CANDIDATE", name="bindingrole")
    op.create_table(
        "route_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.Enum("ACTIVE", "DISABLED", name="routegroupstatus"), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_route_groups_code", "route_groups", ["code"])
    op.create_table(
        "route_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route_group_id", sa.Integer(), sa.ForeignKey("route_groups.id"), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("transport_providers.id"), nullable=False),
        sa.Column("transport_endpoint_id", sa.Integer(), sa.ForeignKey("transport_endpoints.id"), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_route_bindings_route_group_id", "route_bindings", ["route_group_id"])
    op.create_table(
        "egress_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_egress_groups_code", "egress_groups", ["code"])
    op.create_table(
        "egress_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("egress_groups.id"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("credential_secret_ref", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("target_capacity_users", sa.Integer(), nullable=False),
        sa.Column("assigned_users", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_egress_endpoints_group_id", "egress_endpoints", ["group_id"])
    op.create_table(
        "route_egress_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route_group_id", sa.Integer(), sa.ForeignKey("route_groups.id"), nullable=False),
        sa.Column("egress_endpoint_id", sa.Integer(), sa.ForeignKey("egress_endpoints.id"), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_route_egress_bindings_route_group_id", "route_egress_bindings", ["route_group_id"])
    op.create_table(
        "config_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route_group_id", sa.Integer(), sa.ForeignKey("route_groups.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("previous_version_id", sa.Integer(), sa.ForeignKey("config_versions.id")),
        sa.UniqueConstraint("route_group_id", "version_no", name="uq_route_version"),
    )
    op.create_index("ix_config_versions_route_group_id", "config_versions", ["route_group_id"])


def downgrade() -> None:
    op.drop_table("config_versions")
    op.drop_table("route_egress_bindings")
    op.drop_table("egress_endpoints")
    op.drop_table("egress_groups")
    op.drop_table("route_bindings")
    op.drop_table("route_groups")
