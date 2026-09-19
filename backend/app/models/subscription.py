from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.billing import Order


def utcnow() -> datetime:
    return datetime.now(UTC)


PRECISE_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class SubscriptionStatus(str, enum.Enum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    GRACE = "GRACE"
    PROVISION_FAILED = "PROVISION_FAILED"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"


class ReconcileState(str, enum.Enum):
    SYNCED = "SYNCED"
    PENDING = "PENDING"
    PENDING_MANUAL = "PENDING_MANUAL"
    FAILED = "FAILED"


class UsagePeriodStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    QUEUED = "QUEUED"
    CLOSED = "CLOSED"


class UsageDeltaReason(str, enum.Enum):
    NORMAL = "normal"
    COUNTER_RESET = "counter_reset"
    BOOTSTRAP = "bootstrap"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    next_plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    accounting_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    token_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus))
    route_group_code: Mapped[str] = mapped_column(String(64))
    egress_group_code: Mapped[str | None] = mapped_column(String(64))
    service_expire_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    current_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage_periods.id", use_alter=True, name="fk_subscriptions_current_period")
    )
    reconcile_state: Mapped[ReconcileState] = mapped_column(
        Enum(ReconcileState), default=ReconcileState.PENDING, index=True
    )
    reconcile_attempts: Mapped[int] = mapped_column(default=0)
    provision_error: Mapped[str | None] = mapped_column(Text)
    last_usage_synced_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    order: Mapped[Order] = relationship(back_populates="subscription", foreign_keys=[order_id])
    current_period: Mapped["UsagePeriod | None"] = relationship(
        foreign_keys=[current_period_id], post_update=True
    )

    @property
    def traffic_quota_bytes(self) -> int:
        return self.current_period.quota_bytes if self.current_period else 0

    @property
    def traffic_used_bytes(self) -> int:
        return self.current_period.used_bytes if self.current_period else 0

    @property
    def current_period_start(self) -> datetime | None:
        return self.current_period.period_start if self.current_period else None

    @property
    def current_period_end(self) -> datetime | None:
        return self.current_period.period_end if self.current_period else None


class UsageSample(Base):
    __tablename__ = "usage_samples"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(64), default="default")
    source_counter: Mapped[int] = mapped_column(BigInteger)
    sampled_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)


class UsagePeriod(Base):
    __tablename__ = "usage_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    period_end: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[UsagePeriodStatus] = mapped_column(Enum(UsagePeriodStatus))
    active_subscription_id: Mapped[int | None] = mapped_column(
        Computed(
            "CASE WHEN status = 'ACTIVE' THEN subscription_id ELSE NULL END",
            persisted=True,
        ),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)


class UsageDelta(Base):
    __tablename__ = "usage_deltas"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "node_id", "to_sample_id", name="uq_usage_delta_sample"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("usage_periods.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(64), default="default")
    from_sample_id: Mapped[int | None] = mapped_column(ForeignKey("usage_samples.id"))
    to_sample_id: Mapped[int] = mapped_column(ForeignKey("usage_samples.id"))
    delta_bytes: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[UsageDeltaReason] = mapped_column(
        Enum(UsageDeltaReason, values_callable=lambda members: [item.value for item in members])
    )


class SubscriptionVersion(Base):
    __tablename__ = "subscription_versions"
    __table_args__ = (
        UniqueConstraint("subscription_id", "version_no", name="uq_subscription_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    version_no: Mapped[int]
    config_hash: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_by: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    reason: Mapped[str] = mapped_column(String(40))
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_versions.id"))


class UsageAlert(Base):
    __tablename__ = "usage_alerts"
    __table_args__ = (UniqueConstraint("period_id", "threshold", name="uq_usage_alert_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("usage_periods.id"), index=True)
    threshold: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
