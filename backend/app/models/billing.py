from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.identity import Customer
    from backend.app.models.subscription import Subscription


def utcnow() -> datetime:
    return datetime.now(UTC)


PRECISE_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class PlanStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    ACTIVATED = "ACTIVATED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PaymentStatus(str, enum.Enum):
    UNPAID = "UNPAID"
    PAID = "PAID"
    REFUNDED = "REFUNDED"


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (CheckConstraint("duration_days = 30", name="ck_plan_fixed_30_day_duration"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_days: Mapped[int]
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    route_group_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[PlanStatus] = mapped_column(Enum(PlanStatus), default=PlanStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("customer_id", "client_request_id", name="uq_order_idempotency"),
        UniqueConstraint("payment_provider", "payment_reference", name="uq_order_payment_receipt"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    target_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", use_alter=True, name="fk_orders_target_subscription")
    )
    addon_bytes: Mapped[int | None] = mapped_column(BigInteger)
    client_request_id: Mapped[str] = mapped_column(String(80))
    order_type: Mapped[str] = mapped_column(String(32), default="PURCHASE")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.UNPAID
    )
    payment_provider: Mapped[str | None] = mapped_column(String(32))
    payment_reference: Mapped[str | None] = mapped_column(String(80))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer] = relationship()
    plan: Mapped[Plan] = relationship()
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="order", uselist=False, foreign_keys="Subscription.order_id"
    )


class SubscriptionChange(Base):
    __tablename__ = "subscription_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(32))
    from_plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    to_plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    traffic_before: Mapped[int]
    traffic_after: Mapped[int]
    expire_before: Mapped[datetime]
    expire_after: Mapped[datetime]
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
