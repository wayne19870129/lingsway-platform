"""Phase 3 transport providers and normalized endpoints.

Revision ID: 0003_transport
Revises: 0002_accounting
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_transport"
down_revision: str | None = "0002_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transport_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider_type", sa.String(40), nullable=False),
        sa.Column("secret_ref", sa.String(160), nullable=False),
        sa.Column("status", sa.Enum("HEALTHY", "WATCH", "LOW_CAPACITY", "EXHAUSTED", "DEGRADED", "OFFLINE", "DISABLED", name="providerstatus"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("purchased_bytes", sa.BigInteger()),
        sa.Column("used_bytes", sa.BigInteger()),
        sa.Column("cycle_end", sa.DateTime(timezone=True)),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("last_health_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transport_providers_code", "transport_providers", ["code"])
    op.create_table(
        "transport_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("transport_providers.id"), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("transport", sa.String(32)),
        sa.Column("tls_mode", sa.String(32)),
        sa.Column("auth_secret_ref", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("packet_loss", sa.Integer()),
        sa.Column("last_health_at", sa.DateTime(timezone=True)),
        sa.Column("raw_metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_id", "external_id", name="uq_transport_endpoint_external"),
    )
    op.create_index("ix_transport_endpoints_provider_id", "transport_endpoints", ["provider_id"])


def downgrade() -> None:
    op.drop_table("transport_endpoints")
    op.drop_table("transport_providers")
