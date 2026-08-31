"""Database-driven traffic rules and circuit-breaker policies.

Revision ID: 0012_dynamic_policy
Revises: 0011_capacity_egress
"""
from typing import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0012_dynamic_policy"
down_revision: str | None = "0011_capacity_egress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GIB = 1024**3


def upgrade() -> None:
    op.create_table(
        "traffic_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_set", sa.String(64), nullable=False),
        sa.Column("match_type", sa.String(32), nullable=False),
        sa.Column("match_value", sa.String(512), nullable=False, server_default=""),
        sa.Column("target_egress", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "rule_set", "match_type", "match_value", name="uq_traffic_rule_match"
        ),
    )
    op.create_index("ix_traffic_rules_rule_set", "traffic_rules", ["rule_set"])
    op.create_table(
        "circuit_breaker_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("scope", sa.String(40), nullable=False),
        sa.Column("window", sa.String(20), nullable=False, server_default="DAILY"),
        sa.Column("threshold_bytes", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint("code", name="uq_circuit_breaker_policy_code"),
    )
    op.create_index(
        "ix_circuit_breaker_policies_code", "circuit_breaker_policies", ["code"]
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    rules = sa.table(
        "traffic_rules",
        sa.column("rule_set", sa.String),
        sa.column("match_type", sa.String),
        sa.column("match_value", sa.String),
        sa.column("target_egress", sa.String),
        sa.column("priority", sa.Integer),
        sa.column("enabled", sa.Boolean),
        sa.column("notes", sa.Text),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(
        rules,
        [{
            "rule_set": "default",
            "match_type": "MATCH",
            "match_value": "",
            "target_egress": "RESIDENTIAL",
            "priority": 1000000,
            "enabled": True,
            "notes": "Preserves the pre-0012 fallback behavior.",
            "created_at": now,
            "updated_at": now,
        }],
    )
    policies = sa.table(
        "circuit_breaker_policies",
        sa.column("code", sa.String),
        sa.column("scope", sa.String),
        sa.column("window", sa.String),
        sa.column("threshold_bytes", sa.BigInteger),
        sa.column("action", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("notes", sa.Text),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(
        policies,
        [
            {
                "code": "SINGLE_USER_WARNING",
                "scope": "SUBSCRIPTION",
                "window": "DAILY",
                "threshold_bytes": 12 * GIB,
                "action": "ALERT",
                "enabled": True,
                "notes": "Single-user daily traffic warning.",
                "created_at": now,
                "updated_at": now,
            },
            {
                "code": "SINGLE_USER_AUTO_DEGRADE",
                "scope": "SUBSCRIPTION",
                "window": "DAILY",
                "threshold_bytes": 20 * GIB,
                "action": "AUTO_DEGRADE",
                "enabled": True,
                "notes": "Policy threshold only; enforcement uses the routing control plane.",
                "created_at": now,
                "updated_at": now,
            },
            {
                "code": "PROVIDER_ACCOUNT_ALL_TIER_WARNING",
                "scope": "TRANSPORT_PROVIDER",
                "window": "PACKAGE",
                "threshold_bytes": 200 * GIB,
                "action": "ALERT_ALL_TIERS",
                "enabled": True,
                "notes": "Provider-account all-tier warning.",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("circuit_breaker_policies")
    op.drop_table("traffic_rules")
