"""Persist provider-neutral payment receipts.

Revision ID: 0014_payment_provider
Revises: 0013_dual_versions
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_payment_provider"
down_revision: str | None = "0013_dual_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("payment_provider", sa.String(32)))
    op.add_column("orders", sa.Column("payment_reference", sa.String(80)))
    op.create_unique_constraint(
        "uq_order_payment_receipt",
        "orders",
        ["payment_provider", "payment_reference"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_order_payment_receipt", "orders", type_="unique")
    op.drop_column("orders", "payment_reference")
    op.drop_column("orders", "payment_provider")
