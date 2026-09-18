from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


PRECISE_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(primary_key=True)
    secret_ref: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TransportCapacityAlert(Base):
    __tablename__ = "transport_capacity_alerts"
    __table_args__ = (
        UniqueConstraint(
            "provider_id", "threshold", "quota_bytes", name="uq_transport_capacity_alert"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("transport_providers.id"), index=True)
    threshold: Mapped[int]
    level: Mapped[str] = mapped_column(String(20))
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)


class ProviderUsageRecord(Base):
    __tablename__ = "provider_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("transport_providers.id"), index=True)
    purchased_bytes: Mapped[int] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger)
    remaining_bytes: Mapped[int] = mapped_column(BigInteger)
    cycle_start: Mapped[datetime]
    cycle_end: Mapped[datetime]
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatewayMetric(Base):
    __tablename__ = "gateway_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_code: Mapped[str] = mapped_column(String(64), index=True)
    cpu_percent: Mapped[int]
    ram_percent: Mapped[int]
    disk_percent: Mapped[int]
    network_in_bps: Mapped[int] = mapped_column(BigInteger)
    network_out_bps: Mapped[int] = mapped_column(BigInteger)
    active_connections: Mapped[int]
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AbuseWatch(Base):
    __tablename__ = "abuse_watch"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="WATCH")
    signal: Mapped[str] = mapped_column(String(50))
    observed_value: Mapped[int] = mapped_column(BigInteger)
    baseline_value: Mapped[int] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    subject: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tutorial(Base):
    __tablename__ = "tutorials"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    client_name: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    body_markdown: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(default=100)
    published: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
