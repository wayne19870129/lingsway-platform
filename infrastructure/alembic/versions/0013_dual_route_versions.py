"""Split transport and egress route version streams.

Revision ID: 0013_dual_versions
Revises: 0012_dynamic_policy
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_dual_versions"
down_revision: str | None = "0012_dynamic_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def version_columns(previous_table: str):
    return (
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "route_group_id", sa.Integer(), sa.ForeignKey("route_groups.id"), nullable=False
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("previous_version_id", sa.Integer(), sa.ForeignKey(f"{previous_table}.id")),
    )


def upgrade() -> None:
    op.create_table(
        "transport_versions",
        *version_columns("transport_versions"),
        sa.UniqueConstraint("route_group_id", "version_no", name="uq_transport_version"),
    )
    op.create_index(
        "ix_transport_versions_route_group_id", "transport_versions", ["route_group_id"]
    )
    op.execute(
        """INSERT INTO transport_versions
           (id, route_group_id, version_no, config_hash, snapshot_json, generated_at,
            generated_by, reason, previous_version_id)
           SELECT id, route_group_id, version_no, config_hash, snapshot_json, generated_at,
                  generated_by, reason, previous_version_id
           FROM config_versions ORDER BY id"""
    )
    op.drop_table("config_versions")
    op.create_table(
        "egress_versions",
        *version_columns("egress_versions"),
        sa.UniqueConstraint("route_group_id", "version_no", name="uq_egress_version"),
    )
    op.create_index("ix_egress_versions_route_group_id", "egress_versions", ["route_group_id"])


def downgrade() -> None:
    op.create_table(
        "config_versions",
        *version_columns("config_versions"),
        sa.UniqueConstraint("route_group_id", "version_no", name="uq_route_version"),
    )
    op.create_index("ix_config_versions_route_group_id", "config_versions", ["route_group_id"])
    op.execute(
        """INSERT INTO config_versions
           (id, route_group_id, version_no, config_hash, snapshot_json, generated_at,
            generated_by, reason, previous_version_id)
           SELECT id, route_group_id, version_no, config_hash, snapshot_json, generated_at,
                  generated_by, reason, previous_version_id
           FROM transport_versions ORDER BY id"""
    )
    op.drop_table("egress_versions")
    op.drop_table("transport_versions")
