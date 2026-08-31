"""Phase 1 business core.

Revision ID: 0001_phase1
Revises: None
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_no", sa.String(32), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("nickname", sa.String(80)),
        sa.Column("status", sa.Enum("ACTIVE", "SUSPENDED", name="customerstatus"), nullable=False),
        sa.Column("role", sa.Enum("CUSTOMER", "ADMIN", "SUPER_ADMIN", name="customerrole"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_no"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_customers_customer_no", "customers", ["customer_no"])
    op.create_index("ix_customers_email", "customers", ["email"])
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("traffic_limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("route_group_code", sa.String(64), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("ACTIVE", "INACTIVE", name="planstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_no", sa.String(32), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("client_request_id", sa.String(80), nullable=False),
        sa.Column("order_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "PAID", "ACTIVATED", "CANCELLED", "REFUNDED", name="orderstatus"), nullable=False),
        sa.Column("payment_status", sa.Enum("UNPAID", "PAID", "REFUNDED", name="paymentstatus"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_id", "client_request_id", name="uq_order_idempotency"),
    )
    op.create_index("ix_orders_order_no", "orders", ["order_no"])
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_no", sa.String(32), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("accounting_user_id", sa.String(128), unique=True),
        sa.Column("status", sa.Enum("PROVISIONING", "ACTIVE", "GRACE", "PROVISION_FAILED", "SUSPENDED", "EXPIRED", name="subscriptionstatus"), nullable=False),
        sa.Column("route_group_code", sa.String(64), nullable=False),
        sa.Column("egress_group_code", sa.String(64)),
        sa.Column("traffic_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("traffic_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("addon_traffic_bytes", sa.BigInteger(), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_expire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provision_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_subscription_no", "subscriptions", ["subscription_no"])
    op.create_index("ix_subscriptions_customer_id", "subscriptions", ["customer_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_customer_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("subscriptions")
    op.drop_table("orders")
    op.drop_table("plans")
    op.drop_table("customers")
