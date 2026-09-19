from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import Table, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend.app.api.public import payment_notice, place_order
from backend.app.api.subscription import (
    _create_renewal_order,
    get_subscription,
    subscription_details,
)
from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.domain.capacity import CapacityExceededError
from backend.app.domain.ordering import Order as DomainOrder
from backend.app.domain.ordering import confirm_payment
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    AuditLog,
    Customer,
    CustomerRole,
    CustomerStatus,
    EgressEndpoint,
    EgressGroup,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    UsagePeriod,
    UsagePeriodStatus,
)
from backend.app.providers.base import CapacityDTO
from backend.app.providers.registry import ProviderRegistry, build_registry
from backend.app.schemas.public import OrderCreate


def test_renewal_order_can_select_a_different_sellable_plan(db_session: Session) -> None:
    actor = customer()
    plan_a = Plan(
        plan_code="PLAN_50GB",
        name="A",
        traffic_limit_bytes=50,
        duration_days=30,
        price=Decimal("30"),
        currency="CNY",
        route_group_code="DEFAULT",
    )
    plan_b = Plan(
        plan_code="PLAN_100GB",
        name="B",
        traffic_limit_bytes=100,
        duration_days=30,
        price=Decimal("50"),
        currency="CNY",
        route_group_code="DEFAULT",
    )
    current_order = Order(
        order_no="ORD-RENEW-CURRENT",
        customer=actor,
        plan=plan_a,
        client_request_id="renew-current-001",
        amount=plan_a.price,
        currency="CNY",
    )
    db_session.add_all([actor, plan_a, plan_b, current_order])
    db_session.flush()
    subscription = Subscription(
        subscription_no="SUB-RENEW",
        customer_id=actor.id,
        plan_id=plan_a.id,
        order_id=current_order.id,
        status=SubscriptionStatus.ACTIVE,
        route_group_code="DEFAULT",
        service_expire_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(subscription)
    db_session.flush()
    order = _create_renewal_order(db_session, actor, subscription, plan_b.id, "renew-plan-b-001")
    assert order.order_type == "RENEWAL"
    assert order.plan_id == plan_b.id


def test_subscription_details_returns_fifo_queued_periods(db_session: Session) -> None:
    actor = customer()
    plan_a = Plan(
        plan_code="PLAN_50GB",
        name="A",
        traffic_limit_bytes=50,
        duration_days=30,
        price=Decimal("30"),
        currency="CNY",
        route_group_code="DEFAULT",
    )
    plan_b = Plan(
        plan_code="PLAN_100GB",
        name="B",
        traffic_limit_bytes=100,
        duration_days=30,
        price=Decimal("50"),
        currency="CNY",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan_a, plan_b])
    db_session.flush()
    order = Order(
        order_no="ORD-QUEUE",
        customer_id=actor.id,
        plan_id=plan_a.id,
        client_request_id="queue-current-001",
        amount=plan_a.price,
        currency="CNY",
    )
    db_session.add(order)
    db_session.flush()
    subscription = Subscription(
        subscription_no="SUB-QUEUE",
        customer_id=actor.id,
        plan_id=plan_a.id,
        order_id=order.id,
        status=SubscriptionStatus.ACTIVE,
        route_group_code="DEFAULT",
        service_expire_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(subscription)
    db_session.flush()
    current = UsagePeriod(
        subscription_id=subscription.id,
        order_id=order.id,
        plan_id=plan_a.id,
        period_start=datetime.now(UTC),
        period_end=datetime.now(UTC) + timedelta(days=30),
        quota_bytes=50,
        status=UsagePeriodStatus.ACTIVE,
    )
    db_session.add(current)
    db_session.flush()
    subscription.current_period_id = current.id
    order_b = Order(
        order_no="ORD-Q-B",
        customer_id=actor.id,
        plan_id=plan_b.id,
        client_request_id="queue-b-001",
        amount=plan_b.price,
        currency="CNY",
    )
    order_c = Order(
        order_no="ORD-Q-C",
        customer_id=actor.id,
        plan_id=plan_a.id,
        client_request_id="queue-c-001",
        amount=plan_a.price,
        currency="CNY",
    )
    db_session.add_all([order_b, order_c])
    db_session.flush()
    first = UsagePeriod(
        subscription_id=subscription.id,
        order_id=order_b.id,
        plan_id=plan_b.id,
        period_start=current.period_end,
        period_end=current.period_end + timedelta(days=30),
        quota_bytes=100,
        status=UsagePeriodStatus.QUEUED,
    )
    second = UsagePeriod(
        subscription_id=subscription.id,
        order_id=order_c.id,
        plan_id=plan_a.id,
        period_start=current.period_end,
        period_end=current.period_end + timedelta(days=60),
        quota_bytes=50,
        status=UsagePeriodStatus.QUEUED,
    )
    db_session.add_all([first, second])
    db_session.commit()
    result = subscription_details(subscription.id, db_session, actor)
    queued_periods = cast(list[dict[str, object]], result["queued_periods"])
    assert [item["plan_name"] for item in queued_periods] == ["B", "A"]
    assert [item["queued_at"] for item in queued_periods] == [
        first.created_at,
        second.created_at,
    ]


def _registry() -> ProviderRegistry:
    """A fresh all-mock registry for direct route-function calls in this
    file, which bypass FastAPI's dependency injection entirely (TASK-T16
    Phase 2B8: route handlers now require an explicit ``registry``
    parameter instead of building one internally)."""
    return build_registry(Settings())


def add_available_egress_endpoint(
    db: Session, code: str = "EGRESS_API_01", *, port: int = 21080
) -> EgressEndpoint:
    group = EgressGroup(code=f"GROUP_{code}", region="US")
    db.add(group)
    db.flush()
    endpoint = EgressEndpoint(
        group_id=group.id,
        code=code,
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref=f"egress/{code}/base",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=port,
    )
    db.add(endpoint)
    db.flush()
    return endpoint


@pytest.fixture
def db_session(request: Any) -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table_names = {
        "audit_logs",
        "customers",
        "egress_endpoints",
        "egress_bindings",
        "egress_groups",
        "jwt_sessions",
        "orders",
        "plans",
        "secrets",
        "subscriptions",
        "usage_periods",
    }
    if request.node.name == "test_renewal_order_can_select_a_different_sellable_plan":
        table_names.remove("egress_bindings")
    tables: list[Table] = [Base.metadata.tables[name] for name in table_names]
    if "egress_bindings" in table_names:
        @event.listens_for(engine, "connect")
        def register_sqlite_if(dbapi_connection: Any, _connection_record: Any) -> None:
            dbapi_connection.create_function(
                "IF",
                3,
                lambda condition, when_true, when_false: (
                    when_true if condition else when_false
                ),
                deterministic=True,
            )

    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


def customer() -> Customer:
    return Customer(
        customer_no="CUS-API-001",
        email="api@example.invalid",
        password_hash="unused",
        status=CustomerStatus.ACTIVE,
        role=CustomerRole.CUSTOMER,
    )


def request_with_ua(user_agent: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/s/token",
            "headers": [(b"user-agent", user_agent.encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "http_version": "1.1",
        }
    )


def test_order_to_payment_notice_state_flow(db_session: Session) -> None:
    actor = customer()
    plan = Plan(
        plan_code="API_PLAN",
        name="API Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan])
    db_session.flush()
    add_available_egress_endpoint(db_session)

    order = place_order(
        OrderCreate(plan_id=plan.id, client_request_id="request-001"),
        db_session,
        actor,
        _registry(),
    )
    assert order.status is OrderStatus.PENDING
    assert order.payment_status is PaymentStatus.UNPAID

    result = payment_notice(
        order.id,
        db_session,
        actor,
        _registry(),
    )
    assert result == {"status": "notified"}
    persisted = db_session.get(Order, order.id)
    assert persisted is not None
    assert persisted.status is OrderStatus.PENDING
    assert persisted.payment_status is PaymentStatus.UNPAID
    assert (
        db_session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == str(order.id), AuditLog.result == "SUCCESS"
            )
        )
        is not None
    )


def test_capacity_is_rejected_before_payment_mark() -> None:
    class State:
        paid = False
        rejected = False

        def mark_paid(self, order_id: str) -> None:
            self.paid = True

        def reject_capacity(self, order_id: str, reason: str) -> None:
            self.rejected = True

    state = State()
    with pytest.raises(CapacityExceededError):
        confirm_payment(
            DomainOrder("order-1", Decimal("50")),
            CapacityDTO(Decimal("60"), Decimal("0"), Decimal("20")),
            state,
        )
    assert state.rejected is True
    assert state.paid is False


def test_place_order_rejects_when_no_dedicated_ip_available(db_session: Session) -> None:
    actor = customer()
    plan = Plan(
        plan_code="NO_IP_PLAN",
        name="No IP Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan])
    db_session.flush()
    # No EgressEndpoint row at all: the read-only IP precheck must reject
    # the order before it ever reaches the database, regardless of GB quota.

    with pytest.raises(HTTPException) as excinfo:
        place_order(
            OrderCreate(plan_id=plan.id, client_request_id="no-ip-request-001"),
            db_session,
            actor,
            _registry(),
        )
    assert excinfo.value.status_code == 409
    assert (
        db_session.scalar(select(Order).where(Order.client_request_id == "no-ip-request-001"))
        is None
    )


def test_place_order_rejects_when_only_mismatched_egress_endpoints_exist(
    db_session: Session,
) -> None:
    """Endpoints that fail exactly one of the four allocate_endpoint filter
    conditions must not be counted as available -- the precheck must match
    that filter exactly, not a looser approximation of it. `capacity` isn't
    varied here: the schema has `CheckConstraint("capacity = 1")`, so a
    capacity mismatch can't exist as real data in the first place."""
    actor = customer()
    plan = Plan(
        plan_code="MISMATCH_PLAN",
        name="Mismatch Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan])
    db_session.flush()

    wrong_status = add_available_egress_endpoint(db_session, code="EGRESS_WRONG_STATUS", port=21081)
    wrong_status.status = "MAINTENANCE"
    wrong_purpose = add_available_egress_endpoint(
        db_session, code="EGRESS_WRONG_PURPOSE", port=21082
    )
    wrong_purpose.purpose = "STAGING"
    occupied = add_available_egress_endpoint(db_session, code="EGRESS_OCCUPIED", port=21083)
    occupied.current_count = 1
    db_session.flush()

    with pytest.raises(HTTPException) as excinfo:
        place_order(
            OrderCreate(plan_id=plan.id, client_request_id="mismatch-request-001"),
            db_session,
            actor,
            _registry(),
        )
    assert excinfo.value.status_code == 409
    assert (
        db_session.scalar(select(Order).where(Order.client_request_id == "mismatch-request-001"))
        is None
    )


def test_place_order_precheck_does_not_mutate_egress_endpoint(db_session: Session) -> None:
    actor = customer()
    plan = Plan(
        plan_code="NO_MUTATE_PLAN",
        name="No Mutate Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan])
    db_session.flush()
    endpoint = add_available_egress_endpoint(db_session)
    endpoint_id = endpoint.id

    place_order(
        OrderCreate(plan_id=plan.id, client_request_id="no-mutate-request-001"),
        db_session,
        actor,
        _registry(),
    )

    db_session.expire_all()
    persisted = db_session.get(EgressEndpoint, endpoint_id)
    assert persisted is not None
    assert persisted.status == "AVAILABLE"
    assert persisted.current_count == 0


def test_subscription_feed_has_complete_private_headers_and_ua_formats(
    db_session: Session,
) -> None:
    actor = customer()
    plan = Plan(
        plan_code="SUB_PLAN",
        name="Subscription Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    db_session.add_all([actor, plan])
    db_session.flush()
    order = Order(
        order_no="ORD-SUB-001",
        customer_id=actor.id,
        plan_id=plan.id,
        client_request_id="subscription-request-001",
        amount=plan.price,
        currency=plan.currency,
    )
    db_session.add(order)
    db_session.flush()
    subscription = Subscription(
        subscription_no="SUB-API-001",
        customer_id=actor.id,
        plan_id=plan.id,
        order_id=order.id,
        accounting_user_id="accounting-user-1",
        status=SubscriptionStatus.ACTIVE,
        route_group_code="DEFAULT",
        service_expire_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(subscription)
    db_session.flush()
    period = UsagePeriod(
        subscription_id=subscription.id,
        order_id=order.id,
        period_start=datetime.now(UTC),
        period_end=datetime.now(UTC) + timedelta(days=30),
        used_bytes=123,
        quota_bytes=50 * 1024**3,
        status=UsagePeriodStatus.ACTIVE,
    )
    db_session.add(period)
    db_session.flush()
    subscription.current_period_id = period.id
    subscription.current_period = period
    state = SqlAlchemyProvisioningState(db_session, subscription.id)
    state.store_subscription_token(str(actor.id), "subscription-token-001")
    db_session.commit()

    for user_agent, expected_content_type in [
        ("clash.meta/1", "text/yaml; charset=utf-8"),
        ("clash meta/1", "text/yaml; charset=utf-8"),
        ("clashmeta/1", "text/yaml; charset=utf-8"),
        ("mihomo/1", "text/yaml; charset=utf-8"),
        ("stash/1", "text/yaml; charset=utf-8"),
        ("v2ray/1", "text/plain; charset=utf-8"),
        ("v2rayN/1", "text/plain; charset=utf-8"),
        ("v2rayng/1", "text/plain; charset=utf-8"),
        ("sing-box/1", "text/plain; charset=utf-8"),
        ("singbox/1", "text/plain; charset=utf-8"),
        ("unknown-client/1", "text/yaml; charset=utf-8"),
    ]:
        response = get_subscription(
            "subscription-token-001", request_with_ua(user_agent), db_session, _registry()
        )
        assert response.headers["content-type"] == expected_content_type
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["etag"].startswith('"')
        assert "subscription-userinfo" in response.headers
        if expected_content_type.startswith("text/plain"):
            assert base64.b64decode(response.body)
