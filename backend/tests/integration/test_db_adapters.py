from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.core.secrets import reveal_secret
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    BindingRole,
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Order,
    Plan,
    RouteBinding,
    RouteGroup,
    Secret,
    Subscription,
    SubscriptionStatus,
    TrafficRule,
    TransportProviderKind,
    TransportProviderRecord,
)
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    EgressEndpointDTO,
    TenantDTO,
)
from backend.app.providers.forwarder.mihomo import MihomoForwarderProvider, MihomoRuntimeError
from ops.forwarder.render_mihomo_config import render_mihomo_document


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


def test_gateway_route_binding_is_idempotent_and_active_unique(
    state_fixture: tuple[Session, SqlAlchemyProvisioningState, EgressEndpoint],
) -> None:
    db, state, endpoint = state_fixture
    dto = EgressEndpointDTO(str(endpoint.id), endpoint.host, endpoint.port)
    first = state.ensure_gateway_route_binding("marzban-user-1", dto)
    db.commit()
    second = state.ensure_gateway_route_binding("marzban-user-1", dto)
    db.commit()

    assert first.id == second.id
    assert db.scalar(
        select(func.count()).select_from(GatewayRouteBinding).where(
            GatewayRouteBinding.subscription_id == state.subscription_id,
            GatewayRouteBinding.released_at.is_(None),
        )
    ) == 1

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


@dataclass
class FailingMihomoRuntime:
    path: Path
    events: list[str] = field(default_factory=list)
    fail_reload_count: int = 1

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
    document = render_mihomo_document(db)
    original = b"rules:\n  - MATCH,OLD\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(original)
    runtime = FailingMihomoRuntime(config_path)
    provider = MihomoForwarderProvider(lambda: document, runtime)
    candidate = provider.render(DesiredForwarderState({}))

    with pytest.raises(MihomoRuntimeError):
        provider.apply(candidate)

    assert config_path.read_bytes() == original
    assert runtime.events == ["backup", "install", "reload", "restore", "reload"]
