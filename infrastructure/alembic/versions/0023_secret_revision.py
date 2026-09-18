"""Add monotonic non-secret revisions to encrypted secrets.

Revision ID: 0023_secret_revision
Revises: 0022_egress_purpose
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_secret_revision"
down_revision: str | None = "0022_egress_purpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("secrets")}
    if "revision" not in columns:
        op.add_column(
            "secrets",
            sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("secrets")}
    if "revision" in columns:
        op.drop_column("secrets", "revision")
