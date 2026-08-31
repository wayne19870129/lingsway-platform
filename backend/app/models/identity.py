from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


PRECISE_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class CustomerStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class CustomerRole(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[CustomerStatus] = mapped_column(
        Enum(CustomerStatus), default=CustomerStatus.ACTIVE
    )
    role: Mapped[CustomerRole] = mapped_column(Enum(CustomerRole), default=CustomerRole.CUSTOMER)
    source: Mapped[str] = mapped_column(String(32), default="WEBSITE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class JwtSession(Base):
    __tablename__ = "jwt_sessions"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    expires_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    revoke_reason: Mapped[str | None] = mapped_column(String(40))


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", "window_start", name="uq_rate_limit_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(40))
    key_hash: Mapped[str] = mapped_column(String(64))
    window_start: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    request_count: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow, onupdate=utcnow)
