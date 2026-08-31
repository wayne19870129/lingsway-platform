"""Add customer-facing metadata to dedicated egress endpoints.

Revision ID: 0021_egress_metadata
Revises: 0020_webshare_creds
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0021_egress_metadata"
down_revision: str | None = "0020_webshare_creds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, length in (("location", 120), ("isp", 120), ("ip_type", 32)):
        op.add_column(
            "egress_endpoints",
            sa.Column(name, sa.String(length=length), nullable=False, server_default="UNKNOWN"),
        )


def downgrade() -> None:
    for name in ("ip_type", "isp", "location"):
        op.drop_column("egress_endpoints", name)
