"""Add the endpoint purpose used by production egress allocation.

Revision ID: 0022_egress_purpose
Revises: 0021_egress_metadata
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0022_egress_purpose"
down_revision: str | None = "0021_egress_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("egress_endpoints")}
    if "purpose" not in columns:
        op.add_column(
            "egress_endpoints",
            sa.Column("purpose", sa.String(length=16), nullable=False, server_default="PRODUCTION"),
        )


def downgrade() -> None:
    op.drop_column("egress_endpoints", "purpose")
