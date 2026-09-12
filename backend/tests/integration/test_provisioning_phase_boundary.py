"""ADR-017: narrowed GatewayRouteBinding named-lock hold span in the
paid-purchase provisioning saga.

Real MySQL 8.4 CI service only (never SQLite/mock) -- same rationale as
``test_gateway_route_binding_lock.py``: this is a real MySQL
``GET_LOCK()``/``RELEASE_LOCK()`` timing contract, which nothing else can
stand in for. These tests exercise the actual production call path
(``services.confirm_payment_and_provision`` -> ``ProvisioningService.
provision_prepare``/``provision_apply_gateway`` -> real
``SqlAlchemyProvisioningState``), not a re-implementation of it, so a
regression in the real wiring would fail these tests too.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.domain.ordering import BillingCommand, BillingOrderType, PaymentConfirmation
from backend.app.domain.provisioning import (
    ExternalTenantCreationError,
    ProvisioningService,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionStatus,
)
from backend.app.infra.gateway_route_lock import (
    GatewayRouteBindingLockError,
    gateway_route_binding_write,
)
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    Customer,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Order,
    Plan,
    Subscription,
)
from backend.app.models.subscription import SubscriptionStatus
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.registry import ProviderRegistry
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider
from backend.app.services import confirm_payment_and_provision


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL is required; DB adapter tests are never skipped")
    sync_url = database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    eng = build_engine(sync_url)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


def _seed(db: Session, *, suffix: str) -> tuple[Order, Subscription, EgressEndpoint]:
    customer = Customer(
        customer_no=f"CUS-PB-{suffix}",
        email=f"pb-{suffix}@example.invalid",
        password_hash="hash",
    )
    plan = Plan(
        plan_code=f"PB-PLAN-{suffix}",
        name="Phase Boundary Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price="30.00",
        currency="USD",
        route_group_code=f"RG_PB_{suffix}",
    )
    group = EgressGroup(code=f"EG-PB-{suffix}", region="US")
    db.add_all([customer, plan, group])
    db.flush()
    order = Order(
        order_no=f"ORD-PB-{suffix}",
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id=f"pb-request-{suffix}",
        order_type="PURCHASE",
        amount="30.00",
        currency="USD",
    )
    db.add(order)
    db.flush()
    subscription = Subscription(
        subscription_no=f"SUB-PB-{suffix}",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=order.id,
        status=SubscriptionStatus.PROVISIONING,
        route_group_code=f"RG_PB_{suffix}",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    endpoint = EgressEndpoint(
        group_id=group.id,
        code=f"EGRESS_PB_{suffix}",
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref=f"egress/pb/{suffix}",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=13000 + hash(suffix) % 900,
    )
    db.add_all([subscription, endpoint])
    db.flush()
    return order, subscription, endpoint


class _FastTimeoutProvisioningState(SqlAlchemyProvisioningState):
    """Only overrides the GET_LOCK() wait-to-acquire timeout to keep the
    lock-acquisition-failure test fast and deterministic regardless of how
    long a contending holder happens to hold the lock -- the fail-closed
    contract this test verifies is identical to the real
    ``DEFAULT_LOCK_TIMEOUT_SECONDS`` (30s) used in production; only the
    wait duration differs. Mirrors the same technique already used in
    ``test_gateway_route_binding_lock.py::test_get_lock_timeout_fails_closed_with_zero_writes``."""

    @contextmanager
    def gateway_route_binding_lock(self) -> Iterator[None]:
        with gateway_route_binding_write(self.db, timeout_seconds=1):
            yield


@dataclass
class _RealCommitOrderState:
    """Minimal ``OrderWorkflowState`` whose ``transaction()`` commits/rolls
    back the *real* session shared with ``state`` (mirrors production,
    where both ``_OrderProvisioningState`` and ``_SqlAlchemyOrderState``
    wrap the same request-scoped ``db: Session``) -- so a successful
    ``GatewayRouteBinding`` flush actually gets committed, and a failure
    actually gets rolled back, exactly as it would in production."""

    db: Session
    events: list[str] = field(default_factory=list)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def record_payment(self, order_id: str, receipt: PaymentConfirmation) -> None:
        self.events.append("paid")

    def reject_capacity(self, order_id: str, reason: str) -> None:
        self.events.append("capacity-rejected")

    def prepare_purchase(self, command: BillingCommand) -> None:
        self.events.append("prepare-purchase")

    def apply_renewal(self, command: BillingCommand) -> None:
        raise AssertionError("not used by purchase-order tests")

    def apply_upgrade(self, command: BillingCommand) -> None:
        raise AssertionError("not used by purchase-order tests")

    def apply_addon(self, command: BillingCommand) -> None:
        raise AssertionError("not used by purchase-order tests")

    def activate_subscription(self, command: BillingCommand) -> None:
        self.events.append("activate")

    def mark_provision_pending(self, command: BillingCommand, error: str) -> None:
        self.events.append("pending-manual")

    def mark_provision_failed(self, command: BillingCommand, error: str) -> None:
        self.events.append("provision-failed")


@dataclass
class _Runs:
    events: list[tuple[str, str]] = field(default_factory=list)
    status: ProvisionStatus | None = None
    error: str | None = None

    def start(self, request: ProvisionRequest) -> str:
        return "run-pb-1"

    def record_step(self, run_id: str, step: Any, status: str) -> None:
        self.events.append((str(step), status))

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None:
        pass

    def mark_status(self, run_id: str, status: ProvisionStatus, error: str | None = None) -> None:
        self.status = status
        self.error = error

    def mark_notification_failed(self, run_id: str, error: str) -> None:
        pass


def _command(order_id: int) -> BillingCommand:
    return BillingCommand(
        order_id=str(order_id),
        subscription_id=None,
        order_type=BillingOrderType.PURCHASE,
    )


def _request(order_id: int, customer_id: int, *, username: str) -> ProvisionRequest:
    return ProvisionRequest(
        order_id=str(order_id),
        customer_id=str(customer_id),
        username=username,
        quota_gb=Decimal("10"),
        thread_limit=10,
        expire_at=datetime(2027, 1, 1, tzinfo=UTC),
        subscription_domain="subs.example.invalid",
    )


def _registry(**overrides: object) -> ProviderRegistry:
    defaults: dict[str, object] = {
        "egress": MockEgressProvider(),
        "accounting": MockAccountingProvider(),
        "gateway": MockGatewayProvider(),
        "forwarder": MockForwarderProvider(),
        "payment": MockPaymentProvider(),
        "notify": NoopNotifyProvider(),
    }
    defaults.update(overrides)
    return ProviderRegistry(
        egress=defaults["egress"],  # type: ignore[arg-type]
        accounting=defaults["accounting"],  # type: ignore[arg-type]
        gateway=defaults["gateway"],  # type: ignore[arg-type]
        forwarder=defaults["forwarder"],  # type: ignore[arg-type]
        payment=defaults["payment"],  # type: ignore[arg-type]
        notify=defaults["notify"],  # type: ignore[arg-type]
        email=NoopEmailProvider(),
        captcha=NoopCaptchaProvider(),
        storage=MockBlobStorage(),
        transport=MockTransportProvider(),
    )


# ---------------------------------------------------------------------------
# 1: steps 1-6 never touch the named lock -- proven by holding it ourselves.
# ---------------------------------------------------------------------------


def test_provision_prepare_never_needs_the_lock_even_while_another_session_holds_it(
    engine: Engine,
) -> None:
    """If provision_prepare() (steps 1-6) ever tried to acquire the named
    lock, this test would time out / raise, because a second, independent
    session holds it for the entire call. It doesn't: this is the direct,
    real-MySQL proof of ADR-017's "steps 1-6 run unlocked" claim."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PREPNOLOCK")
        seed_db.commit()
        order_id, subscription_id, customer_id = order.id, subscription.id, subscription.customer_id

    with (
        Session(engine) as holder_db,
        gateway_route_binding_write(holder_db, timeout_seconds=1),
        Session(engine) as db,
    ):
        state = SqlAlchemyProvisioningState(db, subscription_id)
        registry = _registry()
        svc = ProvisioningService(
            egress=registry.egress,
            accounting=registry.accounting,
            gateway=registry.gateway,
            forwarder=registry.forwarder,
            notify=registry.notify,
            state=state,
            runs=_Runs(),
        )
        prepared = svc.provision_prepare(_request(order_id, customer_id, username="prep-nolock"))
        assert not isinstance(prepared, ProvisionOutcome)
        assert prepared.run_id == "run-pb-1"


# ---------------------------------------------------------------------------
# 2-3: named lock acquired only for phase B, held through mutation until
#      commit, released only after.
# ---------------------------------------------------------------------------


def test_full_success_path_holds_lock_from_apply_gateway_through_commit(engine: Engine) -> None:
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="FULLSUCCESS")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    checkpoint = threading.Event()
    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def contend() -> None:
        checkpoint.wait(timeout=5)
        with Session(engine) as session:
            try:
                with gateway_route_binding_write(session, timeout_seconds=1):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    contender_thread = threading.Thread(target=contend)
    contender_thread.start()

    class PausingGateway(MockGatewayProvider):
        def apply(self, candidate):  # type: ignore[no-untyped-def]
            result = super().apply(candidate)
            checkpoint.set()
            assert contender_done.wait(timeout=10), "contender never finished its attempt"
            # The contender must have failed: GatewayRouteBinding was just
            # flushed (not yet committed) and the named lock is still ours.
            assert contender_result["acquired"] is False
            return result

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        order_state = _RealCommitOrderState(db)
        runs = _Runs()

        outcome = confirm_payment_and_provision(
            _command(order_id),
            _request(order_id, customer_id, username="full-success"),
            object(),
            state,
            runs,
            order_state,
            "receipt-full-success",
            providers=_registry(gateway=PausingGateway()),
        )
    contender_thread.join(timeout=15)

    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert runs.status is ProvisionStatus.SUCCEEDED
    assert "activate" in order_state.events

    # After the whole call returns (lock released, commit done), a fresh
    # acquisition must succeed immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass
    with Session(engine) as db:
        binding = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is not None
        assert binding.enabled is True


def test_gateway_failure_rolls_back_before_releasing_lock(engine: Engine) -> None:
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="GWFAIL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    checkpoint = threading.Event()
    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def contend() -> None:
        checkpoint.wait(timeout=5)
        with Session(engine) as session:
            try:
                with gateway_route_binding_write(session, timeout_seconds=1):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    contender_thread = threading.Thread(target=contend)
    contender_thread.start()

    class PausingFailingGateway(MockGatewayProvider):
        def apply(self, candidate):  # type: ignore[no-untyped-def]
            checkpoint.set()
            assert contender_done.wait(timeout=10), "contender never finished its attempt"
            assert contender_result["acquired"] is False
            raise RuntimeError("gateway apply exploded")

    accounting = MockAccountingProvider()
    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        order_state = _RealCommitOrderState(db)
        runs = _Runs()

        with pytest.raises(RuntimeError, match="gateway apply exploded"):
            confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="gw-fail"),
                object(),
                state,
                runs,
                order_state,
                "receipt-gw-fail",
                providers=_registry(gateway=PausingFailingGateway(), accounting=accounting),
            )
    contender_thread.join(timeout=15)

    assert runs.status is ProvisionStatus.FAILED
    assert "provision-failed" in order_state.events
    assert accounting.disabled_users == {"gw-fail"}

    # No partial GatewayRouteBinding row must have survived the rollback.
    with Session(engine) as db:
        binding = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is None

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# 4: GET_LOCK() itself failing (timeout) -- zero writes, disable, fail closed.
# ---------------------------------------------------------------------------


def test_lock_acquisition_timeout_fails_closed_with_zero_gateway_writes(engine: Engine) -> None:
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="LOCKTIMEOUT")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with Session(engine) as session, gateway_route_binding_write(session, timeout_seconds=5):
            holder_ready.set()
            release_holder.wait(timeout=10)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_ready.wait(timeout=5), "background holder never acquired the lock"

    accounting = MockAccountingProvider()
    try:
        with Session(engine) as db:
            state = _FastTimeoutProvisioningState(db, subscription_id)
            order_state = _RealCommitOrderState(db)
            runs = _Runs()

            with pytest.raises(GatewayRouteBindingLockError):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="lock-timeout"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-lock-timeout",
                    providers=_registry(accounting=accounting),
                    settings=None,
                )
    finally:
        release_holder.set()
        holder_thread.join(timeout=15)

    assert runs.status is ProvisionStatus.FAILED
    assert "provision-failed" in order_state.events
    assert accounting.disabled_users == {"lock-timeout"}

    with Session(engine) as db:
        assert (
            db.scalar(
                select(GatewayRouteBinding).where(
                    GatewayRouteBinding.subscription_id == subscription_id
                )
            )
            is None
        )

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# 5-6: pre-lock failures never acquire the named lock.
# ---------------------------------------------------------------------------


def test_pending_manual_never_acquires_the_named_lock(engine: Engine) -> None:
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PENDINGMANUAL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    class PartiallyCreatedEgress(MockEgressProvider):
        def create_tenant(self, label, quota_gb, thread_limit):  # type: ignore[no-untyped-def]
            raise ExternalTenantCreationError("provider response lost", "external-1")

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        order_state = _RealCommitOrderState(db)
        runs = _Runs()

        outcome = confirm_payment_and_provision(
            _command(order_id),
            _request(order_id, customer_id, username="pending-manual"),
            object(),
            state,
            runs,
            order_state,
            "receipt-pending-manual",
            providers=_registry(egress=PartiallyCreatedEgress()),
        )

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert "pending-manual" in order_state.events

    # If this path had acquired the lock and left it held, the following
    # acquisition (short timeout) would fail.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=2):
        pass


def test_accounting_create_failure_never_acquires_the_named_lock(engine: Engine) -> None:
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ACCTFAIL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider(failures={"create_user": RuntimeError("acct down")})
    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        order_state = _RealCommitOrderState(db)
        runs = _Runs()

        with pytest.raises(RuntimeError, match="acct down"):
            confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="acct-fail"),
                object(),
                state,
                runs,
                order_state,
                "receipt-acct-fail",
                providers=_registry(accounting=accounting),
            )

    assert runs.status is ProvisionStatus.FAILED
    assert "provision-failed" in order_state.events
    assert accounting.disabled_users == {"acct-fail"}

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=2):
        pass
