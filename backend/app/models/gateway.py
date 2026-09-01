from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Computed,
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


class RouteGroupStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class BindingRole(str, enum.Enum):
    PRIMARY = "PRIMARY"
    BACKUP = "BACKUP"
    CANDIDATE = "CANDIDATE"


class RouteGroup(Base):
    __tablename__ = "route_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[RouteGroupStatus] = mapped_column(
        Enum(RouteGroupStatus), default=RouteGroupStatus.ACTIVE
    )
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RouteBinding(Base):
    __tablename__ = "route_bindings"
    __table_args__ = (
        UniqueConstraint("route_group_id", "provider_id", name="uq_route_binding_provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    route_group_id: Mapped[int] = mapped_column(ForeignKey("route_groups.id"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("transport_providers.id"))
    transport_endpoint_id: Mapped[int | None] = mapped_column(ForeignKey("transport_endpoints.id"))
    role: Mapped[BindingRole] = mapped_column(Enum(BindingRole))
    priority: Mapped[int] = mapped_column(default=100)
    weight: Mapped[int] = mapped_column(default=100)
    enabled: Mapped[bool] = mapped_column(default=True)
    active_primary_route_group_id: Mapped[int | None] = mapped_column(
        Computed(
            "CASE WHEN enabled = 1 AND role = 'PRIMARY' THEN route_group_id ELSE NULL END",
            persisted=True,
        ),
        unique=True,
    )
    health_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GatewayRouteBinding(Base):
    """Maps one accounting user principal to exactly one Egress outbound."""

    __tablename__ = "gateway_route_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    egress_id: Mapped[int] = mapped_column(ForeignKey("egress_endpoints.id"), index=True)
    gateway_principal: Mapped[str] = mapped_column(String(128), index=True)
    outbound_tag: Mapped[str] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    released_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    active_subscription_id: Mapped[int | None] = mapped_column(
        Computed("IF(released_at IS NULL AND enabled = 1, subscription_id, NULL)", persisted=True),
        unique=True,
    )
    active_egress_id: Mapped[int | None] = mapped_column(
        Computed("IF(released_at IS NULL AND enabled = 1, egress_id, NULL)", persisted=True),
        unique=True,
    )
    active_gateway_principal: Mapped[str | None] = mapped_column(
        String(128),
        Computed(
            "IF(released_at IS NULL AND enabled = 1, gateway_principal, NULL)", persisted=True
        ),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow, onupdate=utcnow)


class TrafficRule(Base):
    __tablename__ = "traffic_rules"
    __table_args__ = (
        UniqueConstraint("rule_set", "match_type", "match_value", name="uq_traffic_rule_match"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_set: Mapped[str] = mapped_column(String(64), index=True)
    match_type: Mapped[str] = mapped_column(String(32))
    match_value: Mapped[str] = mapped_column(String(512), default="")
    target_egress: Mapped[str] = mapped_column(String(20))
    priority: Mapped[int] = mapped_column(default=100)
    enabled: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow, onupdate=utcnow)


class CircuitBreakerPolicy(Base):
    __tablename__ = "circuit_breaker_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(40))
    window: Mapped[str] = mapped_column(String(20), default="DAILY")
    threshold_bytes: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, default=utcnow, onupdate=utcnow)


class TransportVersion(Base):
    __tablename__ = "transport_versions"
    __table_args__ = (
        UniqueConstraint("route_group_id", "version_no", name="uq_transport_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    route_group_id: Mapped[int] = mapped_column(ForeignKey("route_groups.id"), index=True)
    version_no: Mapped[int]
    config_hash: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_by: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    reason: Mapped[str] = mapped_column(String(40))
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("transport_versions.id"))


class EgressVersion(Base):
    __tablename__ = "egress_versions"
    __table_args__ = (UniqueConstraint("route_group_id", "version_no", name="uq_egress_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    route_group_id: Mapped[int] = mapped_column(ForeignKey("route_groups.id"), index=True)
    version_no: Mapped[int]
    config_hash: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_by: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    reason: Mapped[str] = mapped_column(String(40))
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("egress_versions.id"))
