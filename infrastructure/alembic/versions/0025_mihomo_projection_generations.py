"""Add append-only Mihomo projection generation evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

PRECISE_DATETIME = sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")

revision: str = "0025_mihomo_projection_generations"
down_revision: str | None = "0024_usage_period_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "mihomo_projection_generations" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "mihomo_projection_generations",
            sa.Column("revision", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("manifest_version", sa.String(32), nullable=False),
            sa.Column("desired_fingerprint", sa.String(64), nullable=False),
            sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        )


def downgrade() -> None:
    if "mihomo_projection_generations" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("mihomo_projection_generations")
