"""Add durable, non-secret transport materialization receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

PRECISE_DATETIME = sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")

revision: str = "0026_mihomo_transport_materializations"
down_revision: str | None = "0025_mihomo_projection_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "mihomo_transport_materializations" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "mihomo_transport_materializations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_record_id", sa.Integer(), nullable=False),
        sa.Column("provider_code", sa.String(40), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("cache_identity", sa.String(160), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("freshness_deadline", PRECISE_DATETIME, nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.Column("updated_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["owner_record_id"], ["transport_providers.id"]),
        sa.UniqueConstraint("owner_record_id", name="uq_mihomo_materialization_owner"),
    )


def downgrade() -> None:
    if "mihomo_transport_materializations" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("mihomo_transport_materializations")
