"""Transport provider kinds, generic secrets, and one active primary.

Revision ID: 0008_transport_ext
Revises: 0007_operations
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_transport_ext"
down_revision: str | None = "0007_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    provider_kind = sa.Enum("SUBSCRIPTION", "SELF_HOSTED", name="transportproviderkind")
    op.add_column(
        "transport_providers",
        sa.Column(
            "kind",
            provider_kind,
            nullable=False,
            server_default="SUBSCRIPTION",
        ),
    )
    op.add_column(
        "transport_providers",
        sa.Column("slug", sa.String(80), nullable=False),
    )
    op.create_index(
        "ix_transport_providers_slug",
        "transport_providers",
        ["slug"],
        unique=True,
    )
    op.drop_column("transport_providers", "provider_type")
    op.alter_column(
        "route_bindings",
        "transport_endpoint_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("secret_ref", sa.String(160), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("secret_ref", name="uq_secrets_secret_ref"),
    )
    op.create_index("ix_secrets_secret_ref", "secrets", ["secret_ref"])
    op.add_column(
        "route_bindings",
        sa.Column(
            "active_primary_route_group_id",
            sa.Integer(),
            sa.Computed(
                "CASE WHEN enabled = 1 AND role = 'PRIMARY' THEN route_group_id ELSE NULL END",
                persisted=True,
            ),
        ),
    )
    op.create_unique_constraint(
        "uq_route_bindings_one_active_primary",
        "route_bindings",
        ["active_primary_route_group_id"],
    )
    op.create_unique_constraint(
        "uq_route_binding_provider",
        "route_bindings",
        ["route_group_id", "provider_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_route_binding_provider", "route_bindings", type_="unique")
    op.drop_constraint(
        "uq_route_bindings_one_active_primary", "route_bindings", type_="unique"
    )
    op.drop_column("route_bindings", "active_primary_route_group_id")
    # The pre-0008 schema required a concrete endpoint. Provider-level bindings
    # cannot be represented there, so discard only those rows during downgrade.
    op.execute("DELETE FROM route_bindings WHERE transport_endpoint_id IS NULL")
    op.alter_column(
        "route_bindings",
        "transport_endpoint_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_index("ix_secrets_secret_ref", table_name="secrets")
    op.drop_table("secrets")
    op.drop_index("ix_transport_providers_slug", table_name="transport_providers")
    op.drop_column("transport_providers", "slug")
    op.add_column(
        "transport_providers",
        sa.Column(
            "provider_type",
            sa.String(40),
            nullable=False,
            server_default="THIRD_PARTY_SUBSCRIPTION",
        ),
    )
    op.drop_column("transport_providers", "kind")
