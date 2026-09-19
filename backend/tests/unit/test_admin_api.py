from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.api import admin as admin_api
from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.dependencies import AuthContext, get_auth_context
from backend.app.domain.capacity import CapacityExceededError
from backend.app.domain.provisioning import ProvisionOutcome, ProvisionStatus
from backend.app.models import (
    Customer,
    CustomerRole,
    CustomerStatus,
    JwtSession,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    UsagePeriod,
    UsagePeriodStatus,
)
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import CapacityDTO
from backend.app.providers.registry import build_registry
from backend.app.schemas.admin import PaymentConfirmation

ADMIN_ENDPOINTS = [
    ("get", "/api/v1/admin/orders"),
    ("get", "/api/v1/admin/customers"),
    ("get", "/api/v1/admin/egress"),
    ("get", "/api/v1/admin/capacity"),
    ("get", "/api/v1/admin/metrics"),
    ("post", "/api/v1/admin/orders/1/confirm-payment"),
    ("get", "/api/v1/admin/accounting/health"),
    ("post", "/api/v1/admin/subscriptions/1/sync-usage"),
]


class _AccountingHealthProbe:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy
        self.calls = 0

    def health_check(self) -> bool:
        self.calls += 1
        return self.healthy


class _TransportHealthProbe:
    def health_check(self) -> bool:
        raise AssertionError("accounting health must not call transport health")


def test_mock_accounting_health_check_is_healthy_by_default() -> None:
    assert MockAccountingProvider().health_check() is True


def test_admin_accounting_health_dispatches_to_accounting_provider() -> None:
    accounting = _AccountingHealthProbe(healthy=True)
    transport = _TransportHealthProbe()
    registry = SimpleNamespace(accounting=accounting, transport=transport)

    result = admin_api.admin_accounting_health(cast(Any, None), cast(Any, registry))

    assert result == {"status": "ok"}
    assert accounting.calls == 1


def _request(client: TestClient, method: str, path: str) -> Any:
    if method == "post" and path.endswith("confirm-payment"):
        return client.post(path, json={"payment_reference": "reference-001"})
    return getattr(client, method)(path)


def _client_with_event_loop_socket(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Permit TestClient's in-process socketpair without permitting network I/O."""
    blocked_create_connection = socket.create_connection
    importlib.reload(socket)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    return TestClient(main.app)


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_every_admin_endpoint_requires_a_token(
    method: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client_with_event_loop_socket(monkeypatch)
    try:
        response = _request(client, method, path)
    finally:
        client.close()
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_every_admin_endpoint_rejects_customer_role(
    method: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    customer = Customer(
        id=1,
        customer_no="CUS-CUSTOMER",
        email="customer@example.invalid",
        password_hash="unused",
        status=CustomerStatus.ACTIVE,
        role=CustomerRole.CUSTOMER,
    )
    session = JwtSession(
        jti="session-customer",
        customer_id=1,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    main.app.dependency_overrides[get_auth_context] = lambda: AuthContext(customer, session)
    client = _client_with_event_loop_socket(monkeypatch)
    try:
        response = _request(client, method, path)
    finally:
        client.close()
        main.app.dependency_overrides.clear()
    assert response.status_code == 403


class _FakeDb:
    def __init__(self, order: Order, plan: Plan, subscription: Subscription) -> None:
        self.order = order
        self.plan = plan
        self.subscription = subscription
        # confirm_payment_and_provision() is monkeypatched in these tests,
        # so _SqlAlchemyProvisionRuns's own methods are never exercised --
        # but its constructor now eagerly opens its own dedicated Session
        # (ADR-017's independent run-status persistence), which needs a
        # real (if trivial) Engine to bind to.
        self._engine = create_engine("sqlite://")

    def get(self, model: type[Any], identifier: int) -> Any:
        if model is Order:
            return self.order if identifier == self.order.id else None
        if model is Plan:
            return self.plan if identifier == self.plan.id else None
        if model is Subscription:
            return self.subscription if identifier == self.subscription.id else None
        return None

    def scalar(self, _statement: Any) -> Subscription:
        return self.subscription

    def refresh(self, _instance: Any) -> None:
        return None

    def commit(self) -> None:
        return None

    def get_bind(self) -> Engine:
        return self._engine


def _provision_fixtures() -> tuple[_FakeDb, Order, Subscription]:
    plan = Plan(
        id=1,
        plan_code="ADMIN-PLAN",
        name="Admin Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="USD",
        route_group_code="DEFAULT",
    )
    order = Order(
        id=1,
        order_no="ORD-ADMIN-001",
        customer_id=2,
        plan_id=1,
        client_request_id="admin-request-001",
        order_type="PURCHASE",
        amount=plan.price,
        currency=plan.currency,
        status=OrderStatus.PENDING,
        payment_status=PaymentStatus.UNPAID,
        created_at=datetime.now(UTC),
    )
    subscription = Subscription(
        id=1,
        subscription_no="SUB-ADMIN-001",
        customer_id=2,
        plan_id=1,
        order_id=1,
        status=SubscriptionStatus.ACTIVE,
        route_group_code="DEFAULT",
        service_expire_at=datetime.now(UTC) + timedelta(days=30),
    )
    return _FakeDb(order, plan, subscription), order, subscription


def test_confirm_payment_success_calls_provisioning_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, order, subscription = _provision_fixtures()
    calls: list[str] = []

    def fake_confirm(*args: object, **kwargs: object) -> ProvisionOutcome:
        del kwargs
        calls.append(type(args[1]).__name__)
        return ProvisionOutcome("run-1", ProvisionStatus.SUCCEEDED, "https://example.invalid/s/token")

    monkeypatch.setattr(admin_api, "confirm_payment_and_provision", fake_confirm)
    result = admin_api.admin_confirm_payment(
        1,
        PaymentConfirmation(payment_reference="reference-001"),
        cast(Session, db),
        Customer(),
        build_registry(Settings()),
    )
    assert calls == ["ProvisionRequest"]
    assert result.subscription_url == "https://example.invalid/s/token"
    assert result.order.id == order.id
    assert result.subscription.id == subscription.id


def test_confirm_payment_capacity_failure_is_rejected_before_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, order, _subscription = _provision_fixtures()

    def fake_confirm(*_args: object, **_kwargs: object) -> ProvisionOutcome:
        del _args, _kwargs
        raise CapacityExceededError(
            capacity=CapacityDTO(
                allocated_gb=Decimal("90"),
                reserved_gb=Decimal("10"),
                total_gb=Decimal("100"),
            ),
            requested_gb=Decimal("1"),
        )

    monkeypatch.setattr(admin_api, "confirm_payment_and_provision", fake_confirm)
    with pytest.raises(HTTPException) as raised:
        admin_api.admin_confirm_payment(
            1,
            PaymentConfirmation(payment_reference="reference-002"),
            cast(Session, db),
            Customer(),
            build_registry(Settings()),
        )
    assert raised.value.status_code == 409
    assert order.status is OrderStatus.PENDING
    assert order.payment_status is PaymentStatus.UNPAID


def test_confirm_renewal_payment_enqueues_without_current_period_side_effects(
    db_session: Session,
) -> None:
    customer = Customer(
        customer_no="CUS-RENEWAL-001",
        email="renewal@example.invalid",
        password_hash="unused",
        status=CustomerStatus.ACTIVE,
        role=CustomerRole.CUSTOMER,
    )
    plan = Plan(
        plan_code="PLAN-RENEWAL",
        name="Renewal Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price=Decimal("30.00"),
        currency="CNY",
        route_group_code="DEFAULT",
    )
    first_order = Order(
        order_no="ORD-RENEWAL-CURRENT",
        customer=customer,
        plan=plan,
        client_request_id="renewal-current-001",
        amount=plan.price,
        currency=plan.currency,
        status=OrderStatus.ACTIVATED,
        payment_status=PaymentStatus.PAID,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([customer, plan, first_order])
    db_session.flush()
    subscription = Subscription(
        subscription_no="SUB-RENEWAL-001",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=first_order.id,
        status=SubscriptionStatus.ACTIVE,
        route_group_code="DEFAULT",
        service_expire_at=datetime.now(UTC) + timedelta(days=20),
        token_hash="stable-token-hash",
    )
    db_session.add(subscription)
    db_session.flush()
    current = UsagePeriod(
        subscription_id=subscription.id,
        order_id=first_order.id,
        plan_id=plan.id,
        period_start=datetime.now(UTC) - timedelta(days=10),
        period_end=datetime.now(UTC) + timedelta(days=20),
        quota_bytes=plan.traffic_limit_bytes,
        used_bytes=123,
        status=UsagePeriodStatus.ACTIVE,
    )
    db_session.add(current)
    db_session.flush()
    subscription.current_period_id = current.id
    renewal = Order(
        order_no="ORD-RENEWAL-NEXT",
        customer_id=customer.id,
        plan_id=plan.id,
        target_subscription_id=subscription.id,
        client_request_id="renewal-next-001",
        order_type="RENEWAL",
        amount=plan.price,
        currency=plan.currency,
        status=OrderStatus.PENDING,
        payment_status=PaymentStatus.UNPAID,
        created_at=datetime.now(UTC),
    )
    db_session.add(renewal)
    db_session.commit()
    db_session.refresh(subscription)
    before_quota = current.quota_bytes
    before_end = current.period_end.replace(tzinfo=None)
    before_token_hash = subscription.token_hash
    before_current_period_id = subscription.current_period_id

    registry = build_registry(Settings())
    accounting = Mock(wraps=registry.accounting)
    accounting.delete_user = Mock()
    registry = replace(registry, accounting=accounting)
    result = admin_api.admin_confirm_payment(
        renewal.id,
        PaymentConfirmation(payment_reference="renewal-reference"),
        db_session,
        Customer(),
        registry,
    )

    db_session.expire_all()
    persisted_subscription = db_session.get(Subscription, subscription.id)
    assert persisted_subscription is not None
    periods = db_session.scalars(
        select(UsagePeriod)
        .where(UsagePeriod.subscription_id == subscription.id)
        .order_by(UsagePeriod.id)
    ).all()
    assert result.order.id == renewal.id
    assert result.subscription.id == subscription.id
    assert len(periods) == 2
    queued = periods[1]
    assert queued.status is UsagePeriodStatus.QUEUED
    assert queued.order_id == renewal.id
    assert periods[0].id == current.id
    assert periods[0].quota_bytes == before_quota
    assert periods[0].period_end == before_end
    assert periods[0].status is UsagePeriodStatus.ACTIVE
    assert persisted_subscription.token_hash == before_token_hash
    assert persisted_subscription.current_period_id == before_current_period_id
    accounting.set_quota.assert_not_called()
    accounting.set_expire.assert_not_called()
    accounting.delete_user.assert_not_called()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table_names = {
        "audit_logs",
        "customers",
        "jobs",
        "orders",
        "plans",
        "subscriptions",
        "usage_periods",
    }
    tables: list[Table] = [Base.metadata.tables[name] for name in table_names]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()
