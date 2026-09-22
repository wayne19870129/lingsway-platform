from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.api.public import place_order
from backend.app.core.config import Settings
from backend.app.core.database import Base, build_engine
from backend.app.core.secrets import reveal_secret
from backend.app.infra.provisioning_state import (
    ProvisioningStateError,
    SqlAlchemyProvisioningState,
)
from backend.app.models import (
    BindingRole,
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    RouteBinding,
    RouteGroup,
    Secret,
    Subscription,
    SubscriptionStatus,
    TrafficRule,
    TransportProviderKind,
    TransportProviderRecord,
    UsagePeriod,
    UsagePeriodStatus,
)
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    EgressEndpointDTO,
    ForwarderListenerDTO,
    HealthReport,
    TenantDTO,
)
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretSnapshot,
    MihomoFinalizationResolvers,
    MihomoForwarderProvider,
    MihomoRollbackVerifiedError,
    MihomoRuntimeError,
)
from backend.app.providers.registry import build_registry
from backend.app.schemas.public import OrderCreate


class FakeControllerSecretResolver:
    def resolve(self, secret_ref: str) -> ControllerSecretSnapshot:
        return ControllerSecretSnapshot(secret_ref, 1, "test-api-secret")

    def resolve_egress_credential(self, secret_ref: str) -> object:
        pytest.fail(f"unexpected egress credential resolution: {secret_ref}")


@pytest.fixture
def db() -> Any:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL is required; DB adapter tests are never skipped")
    sync_url = database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    engine = build_engine(sync_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
        session.rollback()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def state_fixture(db: Session) -> tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint]:
    customer = Customer(
        customer_no="CUS-ADAPTER-001",
        email="adapter@example.invalid",
        password_hash="hash",
    )
    plan = Plan(
        plan_code="ADAPTER-PLAN",
        name="Adapter Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price="30.00",
        currency="USD",
        route_group_code="RG_ADAPTER",
    )
    group = EgressGroup(code="EG-ADAPTER", region="US")
    db.add_all([customer, plan, group])
    db.flush()
    order = Order(
        order_no="ORD-ADAPTER-001",
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id="adapter-request-001",
        order_type="PURCHASE",
        amount="30.00",
        currency="USD",
    )
    db.add(order)
    db.flush()
    subscription = Subscription(
        subscription_no="SUB-ADAPTER-001",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=order.id,
        status=SubscriptionStatus.PROVISIONING,
        route_group_code="RG_ADAPTER",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    endpoint = EgressEndpoint(
        group_id=group.id,
        code="EGRESS_ADAPTER_01",
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref="egress/adapter/base",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=11080,
    )
    db.add_all([subscription, endpoint])
    db.flush()
    return db, SqlAlchemyProvisioningState(db, subscription.id), endpoint


def test_credentials_are_encrypted_and_referenced_by_egress_binding(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
) -> None:
    db, state, endpoint = state_fixture
    secret_ref = state.save_credentials(
        TenantDTO("tenant-1", "SUB-ADAPTER-001", 50, 100),
        EgressEndpointDTO(str(endpoint.id), endpoint.host, endpoint.port),
        CredentialDTO("proxy-user", "proxy-password"),
    )
    db.commit()

    stored = db.scalar(select(Secret).where(Secret.secret_ref == secret_ref))
    binding = db.scalar(
        select(EgressBinding).where(EgressBinding.subscription_id == state.subscription_id)
    )
    assert stored is not None
    assert stored.ciphertext != '{"username":"proxy-user","password":"proxy-password"}'
    assert reveal_secret(db, secret_ref) == '{"username":"proxy-user","password":"proxy-password"}'
    assert binding is not None
    assert binding.credential_secret_ref == secret_ref


def test_subscription_token_is_hash_plus_encrypted_secret_and_round_trips(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
) -> None:
    db, state, _endpoint = state_fixture
    raw_token = "one-time-token-only-in-test"
    subscription = db.get(Subscription, state.subscription_id)
    assert subscription is not None
    state.store_subscription_token(str(subscription.customer_id), raw_token)
    db.commit()

    subscription = db.get(Subscription, state.subscription_id)
    assert subscription is not None
    stored = db.scalar(
        select(Secret).where(Secret.secret_ref == f"subscription/{state.subscription_id}/token")
    )
    assert subscription.token_hash is not None
    assert raw_token not in subscription.token_hash
    assert stored is not None
    assert raw_token not in stored.ciphertext
    assert state.read_subscription_token() == raw_token
    assert state.current_subscription_url("subs.example.invalid") == (
        "https://subs.example.invalid/s/one-time-token-only-in-test"
    )


def _queue_fixture(db: Session, suffix: str) -> tuple[Subscription, Plan, list[Order]]:
    customer = Customer(
        customer_no=f"CUS-QUEUE-{suffix}",
        email=f"queue-{suffix}@example.invalid",
        password_hash="x",
    )
    plan = Plan(
        plan_code=f"QUEUE-PLAN-{suffix}",
        name="Queue plan",
        traffic_limit_bytes=100,
        duration_days=30,
        price="10.00",
        currency="CNY",
        route_group_code="QUEUE",
    )
    db.add_all([customer, plan])
    db.flush()
    orders = [
        Order(
            order_no=f"ORD-QUEUE-{suffix}-{index}",
            customer_id=customer.id,
            plan_id=plan.id,
            client_request_id=f"queue-{suffix}-{index}",
            amount="10.00",
            currency="CNY",
        )
        for index in range(3)
    ]
    db.add_all(orders)
    db.flush()
    subscription = Subscription(
        subscription_no=f"SUB-QUEUE-{suffix}",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=orders[0].id,
        status=SubscriptionStatus.ACTIVE,
        route_group_code="QUEUE",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    db.add(subscription)
    db.flush()
    return subscription, plan, orders


def test_usage_period_allows_one_active_and_multiple_queued(db: Session) -> None:
    subscription, plan, orders = _queue_fixture(db, "001")
    periods = [
        UsagePeriod(
            subscription_id=subscription.id,
            order_id=order.id,
            plan_id=plan.id,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 2, 1, tzinfo=UTC),
            quota_bytes=100,
            status=status,
        )
        for order, status in zip(
            orders,
            [
                UsagePeriodStatus.ACTIVE,
                UsagePeriodStatus.QUEUED,
                UsagePeriodStatus.QUEUED,
            ],
            strict=True,
        )
    ]
    db.add_all(periods)
    db.flush()
    subscription.current_period_id = periods[0].id
    db.commit()
    second_active_order = Order(
        order_no="ORD-QUEUE-001-ACTIVE-2",
        customer_id=subscription.customer_id,
        plan_id=plan.id,
        client_request_id="queue-001-active-2",
        amount="10.00",
        currency="CNY",
    )
    db.add(second_active_order)
    db.flush()
    db.add(
        UsagePeriod(
            subscription_id=subscription.id,
            order_id=second_active_order.id,
            plan_id=plan.id,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 2, 1, tzinfo=UTC),
            quota_bytes=100,
            status=UsagePeriodStatus.ACTIVE,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert db.scalar(
        select(func.count())
        .select_from(UsagePeriod)
        .where(
            UsagePeriod.subscription_id == subscription.id,
            UsagePeriod.status == UsagePeriodStatus.ACTIVE,
        )
    ) == 1
    assert db.scalar(
        select(func.count())
        .select_from(UsagePeriod)
        .where(
            UsagePeriod.subscription_id == subscription.id,
            UsagePeriod.status == UsagePeriodStatus.QUEUED,
        )
    ) == 2


def test_usage_period_order_id_is_required_and_unique(db: Session) -> None:
    subscription, plan, orders = _queue_fixture(db, "002")
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(
            UsagePeriod(
                subscription_id=subscription.id,
                order_id=None,
                plan_id=plan.id,
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 2, 1, tzinfo=UTC),
                quota_bytes=100,
                status=UsagePeriodStatus.ACTIVE,
            )
        )
        db.flush()
    db.rollback()
    db.add(
        UsagePeriod(
            subscription_id=subscription.id,
            order_id=orders[0].id,
            plan_id=plan.id,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 2, 1, tzinfo=UTC),
            quota_bytes=100,
            status=UsagePeriodStatus.ACTIVE,
        )
    )
    db.commit()
    db.add(
        UsagePeriod(
            subscription_id=subscription.id,
            order_id=orders[0].id,
            plan_id=plan.id,
            period_start=datetime(2026, 2, 1, tzinfo=UTC),
            period_end=datetime(2026, 3, 1, tzinfo=UTC),
            quota_bytes=100,
            status=UsagePeriodStatus.QUEUED,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_concurrent_rollover_keeps_exactly_one_active_period(db: Session) -> None:
    subscription, plan, orders = _queue_fixture(db, "003")
    subscription_id = subscription.id
    active = UsagePeriod(
        subscription_id=subscription.id,
        order_id=orders[0].id,
        plan_id=plan.id,
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 2, 1, tzinfo=UTC),
        quota_bytes=100,
        used_bytes=100,
        status=UsagePeriodStatus.ACTIVE,
    )
    queued = [
        UsagePeriod(
            subscription_id=subscription.id,
            order_id=orders[index].id,
            plan_id=plan.id,
            period_start=datetime(2026, 2 + index, 1, tzinfo=UTC),
            period_end=datetime(2026, 3 + index, 1, tzinfo=UTC),
            quota_bytes=100 + index,
            status=UsagePeriodStatus.QUEUED,
        )
        for index in (1, 2)
    ]
    db.add_all([active, *queued])
    db.flush()
    subscription.current_period_id = active.id
    db.commit()
    engine = db.get_bind()

    def rollover() -> None:
        with Session(engine) as contender:
            current = contender.scalar(
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .with_for_update()
            )
            assert current is not None
            from backend.app.workers.accounting_sync import apply_expiry_policy

            apply_expiry_policy(contender, current, datetime(2026, 2, 1, tzinfo=UTC))
            contender.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rollover) for _ in range(2)]
        for future in futures:
            future.result()
    with Session(engine) as final_db:
        final_subscription = final_db.get(Subscription, subscription_id)
        assert final_subscription is not None
        final_periods = final_db.scalars(
            select(UsagePeriod)
            .where(UsagePeriod.subscription_id == subscription_id)
            .order_by(UsagePeriod.id)
        ).all()
        assert sum(period.status is UsagePeriodStatus.ACTIVE for period in final_periods) == 1
        assert final_periods[0].status is UsagePeriodStatus.CLOSED
        assert final_periods[1].status is UsagePeriodStatus.ACTIVE
        assert final_periods[2].status is UsagePeriodStatus.QUEUED
        assert final_subscription.current_period_id == final_periods[1].id


def test_gateway_route_binding_is_idempotent_and_active_unique(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
) -> None:
    db, state, endpoint = state_fixture
    dto = EgressEndpointDTO(str(endpoint.id), endpoint.host, endpoint.port)
    with state.gateway_route_binding_lock():
        first = state.ensure_gateway_route_binding("marzban-user-1", dto)
        db.commit()
    with state.gateway_route_binding_lock():
        second = state.ensure_gateway_route_binding("marzban-user-1", dto)
        db.commit()

    assert first.id == second.id
    assert (
        db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(
                GatewayRouteBinding.subscription_id == state.subscription_id,
                GatewayRouteBinding.released_at.is_(None),
            )
        )
        == 1
    )

    duplicate = GatewayRouteBinding(
        subscription_id=state.subscription_id,
        egress_id=endpoint.id,
        gateway_principal="different-principal",
        outbound_tag="different-outbound",
        enabled=True,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_ip_precheck_pass_does_not_prevent_authoritative_recheck_from_rejecting(
    db: Session,
) -> None:
    """TASK-T15: the order-time IP precheck is a plain read-only COUNT and
    cannot reserve the endpoint it saw available. Two subscriptions race for
    the single available EgressEndpoint: the precheck reports it as
    available to both, but only the locking, authoritative allocation in
    SqlAlchemyProvisioningState.allocate_endpoint (invoked from
    confirm-payment) can actually hand it out, and it must reject the second
    caller instead of double-assigning the same IP.
    """
    customer = Customer(
        customer_no="CUS-RACE-001",
        email="race@example.invalid",
        password_hash="hash",
    )
    plan = Plan(
        plan_code="RACE-PLAN",
        name="Race Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price="30.00",
        currency="USD",
        route_group_code="RG_RACE",
    )
    group = EgressGroup(code="EG-RACE", region="US")
    db.add_all([customer, plan, group])
    db.flush()
    order_a = Order(
        order_no="ORD-RACE-A",
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id="race-request-a",
        order_type="PURCHASE",
        amount="30.00",
        currency="USD",
    )
    order_b = Order(
        order_no="ORD-RACE-B",
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id="race-request-b",
        order_type="PURCHASE",
        amount="30.00",
        currency="USD",
    )
    db.add_all([order_a, order_b])
    db.flush()
    subscription_a = Subscription(
        subscription_no="SUB-RACE-A",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=order_a.id,
        status=SubscriptionStatus.PROVISIONING,
        route_group_code="RG_RACE",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    subscription_b = Subscription(
        subscription_no="SUB-RACE-B",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=order_b.id,
        status=SubscriptionStatus.PROVISIONING,
        route_group_code="RG_RACE",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    endpoint = EgressEndpoint(
        group_id=group.id,
        code="EGRESS_RACE_01",
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref="egress/race/base",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=11090,
    )
    db.add_all([subscription_a, subscription_b, endpoint])
    db.flush()

    # Both orders pass the order-time precheck: exactly one IP is available.
    available_ip_count = db.scalar(
        select(func.count())
        .select_from(EgressEndpoint)
        .where(
            EgressEndpoint.status == "AVAILABLE",
            EgressEndpoint.purpose == "PRODUCTION",
            EgressEndpoint.capacity == 1,
            EgressEndpoint.current_count == 0,
        )
    )
    assert (available_ip_count or 0) > 0

    state_a = SqlAlchemyProvisioningState(db, subscription_a.id)
    state_a.allocate_endpoint(str(customer.id))  # takes the only endpoint
    db.flush()

    state_b = SqlAlchemyProvisioningState(db, subscription_b.id)
    with pytest.raises(ProvisioningStateError):
        state_b.allocate_endpoint(str(customer.id))

    # Order B must never be marked PAID off the back of a failed allocation.
    assert order_b.payment_status is PaymentStatus.UNPAID
    assert order_b.status is OrderStatus.PENDING


def test_place_order_precheck_does_not_reserve_or_bind_the_endpoint(db: Session) -> None:
    """TASK-T15: the order-time precheck is read-only. Calling place_order
    (which runs the precheck and, on success, persists only the Order row)
    must not change the EgressEndpoint it saw, and must not create an
    EgressBinding -- binding only happens later, at confirm-payment time,
    via SqlAlchemyProvisioningState.allocate_endpoint. EgressBinding has
    MySQL-only generated columns, so this assertion needs the real MySQL
    integration DB, unlike the sqlite-backed unit tests for this same
    precheck.
    """
    customer = Customer(
        customer_no="CUS-NOMUTATE-001",
        email="nomutate@example.invalid",
        password_hash="hash",
    )
    plan = Plan(
        plan_code="NOMUTATE-PLAN",
        name="No Mutate Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price="30.00",
        currency="USD",
        route_group_code="RG_NOMUTATE",
    )
    group = EgressGroup(code="EG-NOMUTATE", region="US")
    db.add_all([customer, plan, group])
    db.flush()
    endpoint = EgressEndpoint(
        group_id=group.id,
        code="EGRESS_NOMUTATE_01",
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref="egress/nomutate/base",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=11091,
    )
    db.add(endpoint)
    db.flush()
    endpoint_id = endpoint.id

    place_order(
        OrderCreate(plan_id=plan.id, client_request_id="nomutate-request-001"),
        db,
        customer,
        build_registry(Settings()),
    )

    db.expire_all()
    persisted = db.get(EgressEndpoint, endpoint_id)
    assert persisted is not None
    assert persisted.status == "AVAILABLE"
    assert persisted.current_count == 0
    assert db.scalar(select(func.count()).select_from(EgressBinding)) == 0


@dataclass
class FailingMihomoRuntime:
    path: Path
    events: list[str] = field(default_factory=list)
    fail_reload_count: int = 1
    health_values: list[bool] = field(default_factory=lambda: [True])
    api_secret: str = "test-api-secret"

    def controller_secret_matches(self, secret: str) -> bool:
        return secret == self.api_secret

    def backup(self) -> Path:
        backup = self.path.with_suffix(".before-test")
        shutil.copy2(self.path, backup)
        self.events.append("backup")
        return backup

    def install(self, document: bytes) -> None:
        self.path.write_bytes(document)
        self.events.append("install")

    def reload(self) -> None:
        self.events.append("reload")
        if self.fail_reload_count:
            self.fail_reload_count -= 1
            raise RuntimeError("simulated Mihomo reload failure")

    def restore(self, backup: Path) -> None:
        shutil.copy2(backup, self.path)
        self.events.append("restore")

    def health(self) -> HealthReport:
        self.events.append("health")
        healthy = self.health_values.pop(0) if self.health_values else True
        return HealthReport(healthy)


def test_mihomo_render_failure_restores_exact_pre_operation_state(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, state, endpoint = state_fixture
    provider_record = TransportProviderRecord(
        code="AIRPORT_ADAPTER",
        slug="airport-adapter",
        name="Adapter Airport",
        kind=TransportProviderKind.SUBSCRIPTION,
        secret_ref="transport/provider/adapter",
    )
    route_group = RouteGroup(code="RG_ADAPTER", name="Adapter Route")
    traffic_rule = TrafficRule(
        rule_set="default",
        match_type="MATCH",
        match_value="",
        target_egress="RESIDENTIAL",
        priority=100,
    )
    db.add_all([provider_record, route_group, traffic_rule])
    db.flush()
    db.add(
        RouteBinding(
            route_group_id=route_group.id,
            provider_id=provider_record.id,
            role=BindingRole.PRIMARY,
            priority=1,
        )
    )
    state.save_credentials(
        TenantDTO("tenant-1", "SUB-ADAPTER-001", 50, 100),
        EgressEndpointDTO(str(endpoint.id), endpoint.host, endpoint.port),
        CredentialDTO("proxy-user", "proxy-password"),
    )
    db.commit()
    monkeypatch.setenv("MIHOMO_API_SECRET", "test-api-secret")
    original = b"rules:\n  - MATCH,OLD\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(original)
    runtime = FailingMihomoRuntime(config_path)
    provider = MihomoForwarderProvider(runtime)
    template = provider.render(
        DesiredForwarderState(
            listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
            rules=({"match": "MATCH", "target": "BLOCK"},),
            deployment_constants={
                "external-controller": "172.30.0.10:9090",
                "api-secret-ref": "mihomo/api-secret",
                "api-secret-revision": 1,
            },
        )
    )

    resolver = FakeControllerSecretResolver()
    candidate = provider.finalize(
        template, MihomoFinalizationResolvers(controller=resolver, egress=resolver)
    )
    with pytest.raises(MihomoRuntimeError):
        provider.apply(candidate)

    assert config_path.read_bytes() == original
    assert runtime.events == [
        "backup",
        "install",
        "reload",
        "restore",
        "reload",
        "health",
    ]


def test_mihomo_unhealthy_post_reload_restores_exact_pre_operation_state(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reload that returns without error but leaves Mihomo unhealthy must
    still be treated as a failed apply -- the previous config is restored
    and the call fails closed, exactly like a raised reload exception."""
    db, state, endpoint = state_fixture
    provider_record = TransportProviderRecord(
        code="AIRPORT_ADAPTER",
        slug="airport-adapter",
        name="Adapter Airport",
        kind=TransportProviderKind.SUBSCRIPTION,
        secret_ref="transport/provider/adapter",
    )
    route_group = RouteGroup(code="RG_ADAPTER", name="Adapter Route")
    traffic_rule = TrafficRule(
        rule_set="default",
        match_type="MATCH",
        match_value="",
        target_egress="RESIDENTIAL",
        priority=100,
    )
    db.add_all([provider_record, route_group, traffic_rule])
    db.flush()
    db.add(
        RouteBinding(
            route_group_id=route_group.id,
            provider_id=provider_record.id,
            role=BindingRole.PRIMARY,
            priority=1,
        )
    )
    state.save_credentials(
        TenantDTO("tenant-1", "SUB-ADAPTER-001", 50, 100),
        EgressEndpointDTO(str(endpoint.id), endpoint.host, endpoint.port),
        CredentialDTO("proxy-user", "proxy-password"),
    )
    db.commit()
    monkeypatch.setenv("MIHOMO_API_SECRET", "test-api-secret")
    original = b"rules:\n  - MATCH,OLD\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(original)
    runtime = FailingMihomoRuntime(config_path, fail_reload_count=0, health_values=[False, True])
    provider = MihomoForwarderProvider(runtime)
    template = provider.render(
        DesiredForwarderState(
            listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
            rules=({"match": "MATCH", "target": "BLOCK"},),
            deployment_constants={
                "external-controller": "172.30.0.10:9090",
                "api-secret-ref": "mihomo/api-secret",
                "api-secret-revision": 1,
            },
        )
    )

    resolver = FakeControllerSecretResolver()
    candidate = provider.finalize(
        template, MihomoFinalizationResolvers(controller=resolver, egress=resolver)
    )
    with pytest.raises(MihomoRollbackVerifiedError, match="MIHOMO_ROLLBACK_VERIFIED"):
        provider.apply(candidate)

    assert config_path.read_bytes() == original
    assert runtime.events == [
        "backup",
        "install",
        "reload",
        "health",
        "restore",
        "reload",
        "health",
    ]
