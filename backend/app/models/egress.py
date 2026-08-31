from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.models.gateway import BindingRole


def utcnow() -> datetime:
    return datetime.now(UTC)


PRECISE_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class ProviderStatus(str, enum.Enum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    LOW_CAPACITY = "LOW_CAPACITY"
    EXHAUSTED = "EXHAUSTED"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"


class TransportProviderKind(str, enum.Enum):
    SUBSCRIPTION = "SUBSCRIPTION"
    SELF_HOSTED = "SELF_HOSTED"


class TransportProviderRecord(Base):
    __tablename__ = "transport_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[TransportProviderKind] = mapped_column(
        Enum(TransportProviderKind), default=TransportProviderKind.SUBSCRIPTION
    )
    secret_ref: Mapped[str] = mapped_column(String(160))
    status: Mapped[ProviderStatus] = mapped_column(
        Enum(ProviderStatus), default=ProviderStatus.OFFLINE
    )
    enabled: Mapped[bool] = mapped_column(default=True)
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    measured_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    cycle_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TransportEndpointRecord(Base):
    __tablename__ = "transport_endpoints"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_id", name="uq_transport_endpoint_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("transport_providers.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    region: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    protocol: Mapped[str] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int]
    transport: Mapped[str | None] = mapped_column(String(32))
    tls_mode: Mapped[str | None] = mapped_column(String(32))
    auth_secret_ref: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    latency_ms: Mapped[int | None]
    packet_loss: Mapped[int | None]
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EgressTransportAssignmentState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    RELEASED = "RELEASED"


class EgressTransportAssignment(Base):
    """Persistent placeholder for future Transport-side node allocation."""

    __tablename__ = "egress_transport_assignments"
    __table_args__ = (
        UniqueConstraint(
            "egress_id",
            "transport_node_id",
            "allocation_generation",
            name="uq_egress_transport_assignment_generation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    egress_id: Mapped[int] = mapped_column(ForeignKey("egress_endpoints.id"), index=True)
    transport_provider_id: Mapped[int] = mapped_column(
        ForeignKey("transport_providers.id"), index=True
    )
    transport_node_id: Mapped[int] = mapped_column(ForeignKey("transport_endpoints.id"), index=True)
    allocation_generation: Mapped[int] = mapped_column(default=1)
    state: Mapped[EgressTransportAssignmentState] = mapped_column(
        Enum(EgressTransportAssignmentState), default=EgressTransportAssignmentState.ACTIVE
    )
    assigned_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    last_latency_ms: Mapped[int | None]
    last_checked_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    unavailable_since: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)


class TransportNodeHealth(Base):
    """Health observations reserved for the future allocation scheduler."""

    __tablename__ = "transport_node_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    transport_provider_id: Mapped[int] = mapped_column(
        ForeignKey("transport_providers.id"), index=True
    )
    transport_node_id: Mapped[int] = mapped_column(ForeignKey("transport_endpoints.id"), index=True)
    latency_ms: Mapped[int | None]
    packet_loss: Mapped[int | None]
    available: Mapped[bool] = mapped_column(default=False)
    measured_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255))


class EgressGroup(Base):
    __tablename__ = "egress_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    region: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EgressEndpoint(Base):
    __tablename__ = "egress_endpoints"
    __table_args__ = (
        CheckConstraint("capacity = 1", name="ck_egress_capacity_one"),
        CheckConstraint("current_count BETWEEN 0 AND 1", name="ck_egress_current_count_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("egress_groups.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    provider_name: Mapped[str] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int]
    protocol: Mapped[str] = mapped_column(String(32))
    location: Mapped[str] = mapped_column(String(120), default="UNKNOWN", server_default="UNKNOWN")
    isp: Mapped[str] = mapped_column(String(120), default="UNKNOWN", server_default="UNKNOWN")
    ip_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN", server_default="UNKNOWN")
    purpose: Mapped[str] = mapped_column(
        String(16), default="PRODUCTION", server_default="PRODUCTION"
    )
    credential_secret_ref: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE")
    capacity: Mapped[int] = mapped_column(default=1)
    current_count: Mapped[int] = mapped_column(default=0)
    mihomo_listen_port: Mapped[int] = mapped_column(unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RouteEgressBinding(Base):
    __tablename__ = "route_egress_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    route_group_id: Mapped[int] = mapped_column(ForeignKey("route_groups.id"), index=True)
    egress_endpoint_id: Mapped[int] = mapped_column(ForeignKey("egress_endpoints.id"))
    role: Mapped[BindingRole] = mapped_column(Enum(BindingRole))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EgressBinding(Base):
    __tablename__ = "egress_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    egress_id: Mapped[int] = mapped_column(ForeignKey("egress_endpoints.id"), index=True)
    # Customer-specific upstream credentials.  The endpoint-level secret is
    # retained as the fallback for unassigned inventory; an active binding
    # must use this override when present.
    credential_secret_ref: Mapped[str | None] = mapped_column(String(200))
    assigned_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    active_subscription_id: Mapped[int | None] = mapped_column(
        Computed(
            "IF(released_at IS NULL, subscription_id, NULL)",
            persisted=True,
        ),
        unique=True,
    )
    active_egress_id: Mapped[int | None] = mapped_column(
        Computed(
            "IF(released_at IS NULL, egress_id, NULL)",
            persisted=True,
        ),
        unique=True,
    )


class WebshareSubuser(Base):
    """The Webshare sub-user and encrypted per-proxy credentials for a subscription."""

    __tablename__ = "webshare_subusers"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="uq_webshare_subuser_subscription"),
        UniqueConstraint("webshare_id", name="uq_webshare_subuser_webshare_id"),
        UniqueConstraint("label", name="uq_webshare_subuser_label"),
        UniqueConstraint("credential_secret_ref", name="uq_webshare_subuser_secret_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    webshare_id: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(128))
    proxy_limit_gb: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    max_thread_count: Mapped[int] = mapped_column(Integer)
    credential_secret_ref: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow, onupdate=utcnow)
