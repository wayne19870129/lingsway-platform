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
from unittest import mock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.api.admin import (
    _OrderProvisioningState,
    _SqlAlchemyOrderState,
    _SqlAlchemyProvisionRuns,
)
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
    Job,
    JobStatus,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    Subscription,
)
from backend.app.models.subscription import SubscriptionStatus
from backend.app.providers.accounting.marzban import MarzbanAccountingProvider
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    AccountingCreateEffect,
    AccountingCreateUserError,
    AccountUserDTO,
    CredentialDTO,
    DesiredRoutingState,
    XrayOutboundDTO,
)
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.registry import ProviderRegistry
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider
from backend.app.services import confirm_payment_and_provision as _confirm_payment_and_provision


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


@dataclass(frozen=True)
class _FakeCredentialResolver:
    def resolve(self, secret_ref: str) -> CredentialDTO:
        return CredentialDTO("resolver-user", "resolver-password")

    def resolve_reality_identity(self) -> XrayRealityConfig:
        return XrayRealityConfig("resolver-private-key", ("resolver-short-id",))


def confirm_payment_and_provision(*args: Any, **kwargs: Any) -> ProvisionOutcome | None:
    """Keep every production-path integration call explicitly resolver-bound."""
    kwargs.setdefault("credential_resolver", _FakeCredentialResolver())
    return _confirm_payment_and_provision(*args, **kwargs)


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
            credential_resolver=_FakeCredentialResolver(),
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


# ---------------------------------------------------------------------------
# Major 1 regression (Work review round 2): GET_LOCK() itself raising a
# generic DBAPI/SQLAlchemy exception -- not just returning 0/NULL -- must
# still be normalized into GatewayRouteBindingLockError and drive the exact
# same fail-closed compensation as a timeout.
# ---------------------------------------------------------------------------


def test_generic_acquisition_exception_rolls_back_phase_a_before_marking_failed(
    engine: Engine,
) -> None:
    """A generic DBAPI/SQLAlchemy exception raised while executing
    GET_LOCK() -- not GET_LOCK() returning 0/NULL -- must still be
    normalized by gateway_route_binding_write() into
    GatewayRouteBindingLockError and trigger identical fail-closed
    compensation: phase-A's already-flushed DB state (EgressEndpoint.
    current_count) rolled back, zero GatewayRouteBinding writes, the
    accounting user disabled, and the run marked FAILED. Verified through
    the real production call path (confirm_payment_and_provision ->
    provision_apply_gateway -> gateway_route_binding_lock()) against real
    MySQL, with only the GET_LOCK() execution itself injected to fail --
    everything else (steps 1-6's real flush, the real rollback) runs for
    real."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="GENERICACQ")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider()
    injected = RuntimeError("simulated DBAPI: connection reset by peer")
    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        order_state = _RealCommitOrderState(db)
        runs = _Runs()

        with (
            mock.patch(
                "backend.app.infra.gateway_route_lock._get_lock",
                side_effect=injected,
            ),
            pytest.raises(GatewayRouteBindingLockError) as excinfo,
        ):
            confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="generic-acq"),
                object(),
                state,
                runs,
                order_state,
                "receipt-generic-acq",
                providers=_registry(accounting=accounting),
            )

    assert excinfo.value.__cause__ is injected

    assert runs.status is ProvisionStatus.FAILED
    assert "provision-failed" in order_state.events
    assert accounting.disabled_users == {"generic-acq"}

    with Session(engine) as db:
        assert (
            db.scalar(
                select(GatewayRouteBinding).where(
                    GatewayRouteBinding.subscription_id == subscription_id
                )
            )
            is None
        )
        # Proves state.rollback_database() actually ran: allocate_endpoint()
        # (steps 1-6, unlocked) flushed current_count=1 for this endpoint;
        # if the rollback had been skipped, or ordered after some other
        # commit, that mutation would have survived.
        rolled_back_endpoint = db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

    # The named lock itself must be free: the injected failure happened
    # before GET_LOCK() ever held anything, and the dedicated lock
    # connection must not have been left in a stale held state.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# Major 2 regression (Work review round 2): the production ProvisionRunStore
# adapter's own FAILED status (and, for a fresh run, its own row) must
# survive the very rollback its own lock-acquisition failure triggers --
# proven with the real production adapters, never the in-memory _Runs fake.
# ---------------------------------------------------------------------------


def test_production_run_store_marks_failed_independently_of_phase_a_rollback(
    engine: Engine,
) -> None:
    """Real ``_SqlAlchemyProvisionRuns``, real ``_OrderProvisioningState`` /
    ``_SqlAlchemyOrderState``, all sharing one real MySQL ``Session`` --
    exactly as ``admin_confirm_payment()`` wires them in production. A
    lock-acquisition failure is manufactured the same way as the previous
    test; a fresh, independent ``Session`` (never the one that rolled back)
    then queries the database to confirm every acceptance criterion the
    review demanded: phase-A business mutations rolled back,
    ``GatewayRouteBinding`` == 0, Order/Subscription reached the
    paid-but-unusable terminal state, the Provision Job row still exists,
    its status is FAILED, and its error is persisted."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="RUNPERSIST")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider()
    injected = RuntimeError("simulated DBAPI: connection reset by peer")
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with (
                mock.patch(
                    "backend.app.infra.gateway_route_lock._get_lock",
                    side_effect=injected,
                ),
                pytest.raises(GatewayRouteBindingLockError),
            ):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="run-persist"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-run-persist",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()

    # A fresh, independent Session -- distinct from the one that just
    # rolled back -- is the only acceptable evidence for this criterion.
    with Session(engine) as verify_db:
        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.PAID
        assert persisted_order.payment_status is PaymentStatus.PAID

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED
        assert persisted_subscription.provision_error == "GatewayRouteBindingLockError"

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.last_error_code is not None
        assert "failed to acquire the gateway route binding lock" in job.last_error_code

    assert accounting.disabled_users == {"run-persist"}

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# Major 2 regression (Work review round 3): run-persistence-ordering
# revision. Production-backed: never the in-memory _Runs fake.
# ---------------------------------------------------------------------------


def test_production_activate_commit_failure_never_leaves_job_falsely_succeeded(
    engine: Engine,
) -> None:
    """Major 2 Case A / review round 3 test 2, production-backed: force
    activate_paid_purchase()'s own business commit to fail *after*
    provision_apply_gateway() already returned successfully (gateway
    applied, subscription token issued, NOTIFY sent). The forbidden
    outcome is Job=SUCCEEDED while the subscription ends up in a failed
    state -- mark_apply_gateway_succeeded() must never be reached when
    the commit it depends on never completed, and services.py's outer
    failure handler must durably mark the run FAILED instead (never leave
    it stuck at RUNNING), using the real `_SqlAlchemyProvisionRuns` +
    `_OrderProvisioningState`/`_SqlAlchemyOrderState` sharing one real
    MySQL Session, exactly as production wires them."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ACTIVATEFAIL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)

        real_commit = db.commit
        calls = {"n": 0}

        def flaky_commit() -> None:
            calls["n"] += 1
            # Commit #1 is record_payment()+prepare_purchase()'s; commit #2
            # is activate_paid_purchase()'s own -- the one this test
            # targets. Every other commit (including fail_paid_purchase()'s,
            # in the outer handler this failure triggers) must go through.
            if calls["n"] == 2:
                raise RuntimeError("simulated activation commit failure")
            real_commit()

        db.commit = flaky_commit  # type: ignore[method-assign]

        try:
            with pytest.raises(RuntimeError, match="simulated activation commit failure"):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="activate-fail"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-activate-fail",
                    providers=_registry(accounting=accounting),
                )
        finally:
            db.commit = real_commit  # type: ignore[method-assign]
            runs.close()

    with Session(engine) as verify_db:
        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        # The forbidden combination this test guards against is
        # Job=SUCCEEDED at the same time as a failed subscription -- the
        # fix (services.py's outer except now calls
        # provisioning.mark_run_failed()) makes this a durable FAILED
        # rather than merely "not SUCCEEDED" (e.g. stuck at RUNNING).
        assert job.status is JobStatus.FAILED

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED
        assert not (
            job.status is JobStatus.SUCCEEDED
            and persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED
        )

        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_accounting_progress_write_failure_never_leaves_user_wrongly_enabled_or_disabled(
    engine: Engine,
) -> None:
    """Major 2 Case B / review round 3 test 3, production-backed: after
    accounting.create_user() succeeds, force the very next progress-write
    commit (CREATE_ACCOUNTING_USER's own `_success()` -> record_step() ->
    this run-store's dedicated commit) to fail. Before the fix, this
    exception would propagate out of provision_prepare() *after*
    CREATE_ACCOUNTING_USER's own except block (the one that calls
    accounting.disable_user()) had already exited normally -- leaving a
    created-and-enabled accounting user with no compensation, while the
    order/subscription still end up marked failed. After the fix,
    record_step() swallows this failure internally (ProvisionRunStore's
    best-effort progress-bookkeeping contract) and the saga simply
    continues -- proving no "failed order + enabled accounting user"
    inconsistency is introduced by a mere audit-write hiccup."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ACCTPROGRESS")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    armed = {"value": False}

    class ArmingAccounting(MockAccountingProvider):
        def create_user(self, username: str, quota_bytes: int, expire_at: Any) -> Any:
            result = super().create_user(username, quota_bytes, expire_at)
            armed["value"] = True
            return result

    accounting = ArmingAccounting()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)

        real_runs_commit = runs.db.commit

        def flaky_runs_commit() -> None:
            if armed["value"]:
                armed["value"] = False
                raise RuntimeError("simulated progress-persistence failure")
            real_runs_commit()

        runs.db.commit = flaky_runs_commit  # type: ignore[method-assign]

        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="acct-progress"),
                object(),
                state,
                runs,
                order_state,
                "receipt-acct-progress",
                providers=_registry(accounting=accounting),
            )
        finally:
            runs.close()

    # The progress-write hiccup must not have derailed anything: the saga
    # completes successfully, and the accounting user this test armed on
    # is never wrongly disabled (create_user() genuinely succeeded).
    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert accounting.disabled_users == set()

    with Session(engine) as verify_db:
        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.ACTIVATED

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.ACTIVE

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_gateway_progress_write_failure_never_bypasses_business_compensation(
    engine: Engine,
) -> None:
    """Major 2 Case B (gateway variant) / review round 3 test 4,
    production-backed: after gateway.apply() succeeds, force the very
    next progress-write commit (APPLY_GATEWAY's own `_success()` ->
    record_step()) to fail. Before the fix this exception would propagate
    out of provision_apply_gateway() *after* its own APPLY_GATEWAY except
    block had already exited normally -- skipping straight past
    activate_paid_purchase() and mark_apply_gateway_succeeded() without
    ever running the accounting-disable/rollback compensation that
    handler is meant to guarantee for any other post-apply failure. After
    the fix, this progress-write failure is swallowed internally and the
    saga proceeds to its real, correct outcome: gateway state committed,
    business transaction semantics untouched by the run-store's own
    transaction design."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="GWPROGRESS")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    armed = {"value": False}

    class ArmingGateway(MockGatewayProvider):
        def apply(self, candidate):  # type: ignore[no-untyped-def]
            result = super().apply(candidate)
            armed["value"] = True
            return result

    accounting = MockAccountingProvider()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)

        real_runs_commit = runs.db.commit

        def flaky_runs_commit() -> None:
            if armed["value"]:
                armed["value"] = False
                raise RuntimeError("simulated progress-persistence failure")
            real_runs_commit()

        runs.db.commit = flaky_runs_commit  # type: ignore[method-assign]

        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="gw-progress"),
                object(),
                state,
                runs,
                order_state,
                "receipt-gw-progress",
                providers=_registry(gateway=ArmingGateway(), accounting=accounting),
            )
        finally:
            runs.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert accounting.disabled_users == set()

    with Session(engine) as verify_db:
        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.ACTIVATED

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.ACTIVE

        binding = verify_db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is not None
        assert binding.enabled is True

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_run_store_marks_succeeded_only_after_business_commit(
    engine: Engine,
) -> None:
    """Major 2 Case A / review round 3 test 5, production-backed: the
    Job's terminal SUCCEEDED status must never be observable before
    activate_paid_purchase()'s own business commit has completed. Paused
    inside gateway.apply() (after its real effect, well before
    activate_paid_purchase()'s eventual commit), an independent session
    must see the Job still short of SUCCEEDED; only once the whole call
    returns does it become SUCCEEDED. Real `_SqlAlchemyProvisionRuns` +
    `_OrderProvisioningState`/`_SqlAlchemyOrderState`, never the in-memory
    _Runs fake."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="SUCCEEDORDER")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    checkpoint = threading.Event()
    proceed = threading.Event()
    observed: dict[str, object] = {}

    class PausingGateway(MockGatewayProvider):
        def apply(self, candidate):  # type: ignore[no-untyped-def]
            result = super().apply(candidate)
            checkpoint.set()
            assert proceed.wait(timeout=10), "test harness never signalled proceed"
            return result

    def observe() -> None:
        checkpoint.wait(timeout=5)
        with Session(engine) as verify_db:
            job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
            observed["status_before_commit"] = job.status if job else None
        proceed.set()

    observer_thread = threading.Thread(target=observe)
    observer_thread.start()

    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="succeed-order"),
                object(),
                state,
                runs,
                order_state,
                "receipt-succeed-order",
                providers=_registry(gateway=PausingGateway()),
            )
        finally:
            runs.close()
    observer_thread.join(timeout=15)

    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert observed["status_before_commit"] is not JobStatus.SUCCEEDED

    with Session(engine) as verify_db:
        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED

        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.ACTIVATED

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.ACTIVE

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# Major regression (Work review round 4): PENDING_MANUAL terminal
# persistence ordering. Production-backed: never the in-memory _Runs fake.
# ---------------------------------------------------------------------------


class _PartiallyCreatedEgress(MockEgressProvider):
    """CREATE_TENANT raises ExternalTenantCreationError, optionally with an
    external_id -- the scenario provision_prepare() cannot continue past
    without human review."""

    def __init__(self, external_id: str | None = "external-pending-1") -> None:
        super().__init__()
        self._external_id = external_id

    def create_tenant(self, label: str, quota_gb: Decimal, thread_limit: int) -> Any:
        raise ExternalTenantCreationError("provider response lost", self._external_id)


def test_production_pending_manual_persists_only_after_business_commit(
    engine: Engine,
) -> None:
    """Review round 4 test A, production-backed: the real
    ExternalTenantCreationError path through confirm_payment_and_provision()
    -- the business mark_provision_pending() commit succeeds, the named
    lock is never acquired (PENDING_MANUAL is reached entirely within
    provision_prepare()), and the run's terminal status ends up `PENDING`
    only after that business commit, never before it."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PENDINGORDER")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="pending-order"),
                object(),
                state,
                runs,
                order_state,
                "receipt-pending-order",
                providers=_registry(egress=_PartiallyCreatedEgress()),
            )
        finally:
            runs.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.pending_manual_error is not None
    assert "provider response lost" in outcome.pending_manual_error

    with Session(engine) as verify_db:
        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.PAID
        assert persisted_order.payment_status is PaymentStatus.PAID

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.provision_error is not None

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.PENDING

    # PENDING_MANUAL never touches the named lock -- a fresh acquisition
    # must succeed immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_pending_manual_business_commit_failure_never_leaves_job_prematurely_pending(
    engine: Engine,
) -> None:
    """Review round 4 test B, production-backed: force
    order_state.mark_provision_pending()'s own business commit to fail.
    The Job must never have been written to PENDING at all -- it must
    still be exactly whatever start() initialized it to (RUNNING)."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PENDBIZFAIL")
        seed_db.commit()
        order_id = order.id
        customer_id = subscription.customer_id

    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)

        real_commit = db.commit
        calls = {"n": 0}

        def flaky_commit() -> None:
            calls["n"] += 1
            # Commit #1 is record_payment()+prepare_purchase()'s; commit #2
            # is mark_provision_pending()'s own -- the one this test
            # targets.
            if calls["n"] == 2:
                raise RuntimeError("simulated pending commit failure")
            real_commit()

        db.commit = flaky_commit  # type: ignore[method-assign]

        try:
            with pytest.raises(RuntimeError, match="simulated pending commit failure"):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="pend-biz-fail"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-pend-biz-fail",
                    providers=_registry(egress=_PartiallyCreatedEgress()),
                )
        finally:
            db.commit = real_commit  # type: ignore[method-assign]
            runs.close()

    with Session(engine) as verify_db:
        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        # The forbidden outcome this test guards against: a durable
        # PENDING status for a business commit that never actually
        # completed. mark_run_pending_manual() is only ever reached after
        # that commit succeeds, so it must never have run here.
        assert job.status is not JobStatus.PENDING
        assert job.status is JobStatus.RUNNING


def test_production_pending_manual_run_persistence_failure_never_flips_business_state_to_failed(
    engine: Engine,
) -> None:
    """Review round 4 test C, production-backed: the business
    mark_provision_pending() commit succeeds first; only *then* is the
    run-store's own mark_status(PENDING_MANUAL, ...) commit forced to
    fail. The business state (order/subscription) must remain in its
    legitimate pending-manual state -- never reinterpreted as
    PROVISION_FAILED just because an unrelated audit write failed."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PENDRUNFAIL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    armed = {"value": False}

    class ArmingOrderState(_SqlAlchemyOrderState):
        def mark_provision_pending(self, command: BillingCommand, error: str) -> None:
            super().mark_provision_pending(command, error)
            armed["value"] = True

    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = ArmingOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)

        real_runs_commit = runs.db.commit

        def flaky_runs_commit() -> None:
            if armed["value"]:
                armed["value"] = False
                raise RuntimeError("simulated PENDING_MANUAL persistence failure")
            real_runs_commit()

        runs.db.commit = flaky_runs_commit  # type: ignore[method-assign]

        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="pend-run-fail"),
                object(),
                state,
                runs,
                order_state,
                "receipt-pend-run-fail",
                providers=_registry(egress=_PartiallyCreatedEgress()),
            )
        finally:
            runs.close()

    # The run-store persistence hiccup must not have surfaced as an
    # exception (mark_run_pending_manual() is best-effort) or changed the
    # returned outcome.
    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL

    with Session(engine) as verify_db:
        persisted_order = verify_db.get(Order, order_id)
        assert persisted_order is not None
        assert persisted_order.status is OrderStatus.PAID
        assert persisted_order.payment_status is PaymentStatus.PAID
        assert persisted_order.status is not OrderStatus.ACTIVATED

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is not SubscriptionStatus.PROVISION_FAILED
        assert persisted_subscription.provision_error is not None

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_pending_manual_preserves_external_id_when_present(
    engine: Engine,
) -> None:
    """Review round 4 test D, production-backed: an
    ExternalTenantCreationError carrying an external_id must still have
    that external_id recorded (best-effort, as record_external_id()
    always has been) even though mark_status() is no longer called from
    inside provision_prepare() itself."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="PENDEXTID")
        seed_db.commit()
        order_id = order.id
        customer_id = subscription.customer_id

    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="pend-ext-id"),
                object(),
                state,
                runs,
                order_state,
                "receipt-pend-ext-id",
                providers=_registry(
                    egress=_PartiallyCreatedEgress(external_id="external-pending-42")
                ),
            )
        finally:
            runs.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL

    with Session(engine) as verify_db:
        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        payload = job.payload_json or "{}"
        assert "external-pending-42" in payload
        assert job.status is JobStatus.PENDING


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B6 (ADR-016 Decision 3): route-identity data-flow plumbing.
# gateway_principal must equal the accounting provider's own
# routing_principal, never accounting_user_id/username, never an egress
# tenant_id. Production-backed: real confirm_payment_and_provision() +
# SqlAlchemyProvisioningState, real MySQL/MariaDB.
# ---------------------------------------------------------------------------


def test_production_gateway_principal_is_the_provider_routing_principal_not_username_or_tenant_id(
    engine: Engine,
) -> None:
    """ADR-016 Decision 3's full data flow, proven end to end through the
    real production call path: AccountingProvider.create_user() returns a
    routing_principal deliberately distinct from both the accounting
    username and the egress tenant_id (MockAccountingProvider's
    `mock-routing.<username>` contract, MockEgressProvider's
    `tenant-<n>` ids) -- after a full successful provisioning run,
    Subscription.accounting_user_id must still equal request.username
    (the accounting bounded context, unchanged), while
    GatewayRouteBinding.gateway_principal must equal the provider's
    routing_principal (the Xray routing bounded context) -- and the two
    persisted values must be different from each other and from the
    egress tenant_id, proving neither was silently substituted for the
    other anywhere in the pipeline."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ROUTEIDENTITY")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider()
    egress = MockEgressProvider()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="alice"),
                object(),
                state,
                runs,
                order_state,
                "receipt-route-identity",
                providers=_registry(accounting=accounting, egress=egress),
            )
        finally:
            runs.close()
        # admin_confirm_payment()'s own post-processing step (ADR-014's
        # documented accounting_user_id timing gap: it is written by the
        # route handler after confirm_payment_and_provision() returns, not
        # by the service function itself) -- replicated here since this
        # test drives confirm_payment_and_provision() directly rather than
        # through the FastAPI route.
        subscription_row = db.scalar(
            select(Subscription).where(Subscription.order_id == order_id)
        )
        assert subscription_row is not None
        if subscription_row.accounting_user_id is None:
            subscription_row.accounting_user_id = "alice"
            db.commit()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED

    expected_routing_principal = accounting.users["alice"].routing_principal
    assert expected_routing_principal == "mock-routing.alice"
    egress_tenant_id = next(iter(egress.tenants))

    # The three identities involved must all be distinct -- otherwise this
    # test could pass by accident even if the pipeline silently conflated
    # two of them.
    assert expected_routing_principal not in {"alice", egress_tenant_id}
    assert egress_tenant_id != "alice"

    with Session(engine) as verify_db:
        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.accounting_user_id == "alice"

        binding = verify_db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is not None
        assert binding.gateway_principal == expected_routing_principal
        assert binding.gateway_principal != persisted_subscription.accounting_user_id
        assert binding.gateway_principal != egress_tenant_id

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_xray_render_uses_routing_principal_as_the_user_routes_key(
    engine: Engine,
) -> None:
    """The other half of ADR-016 Decision 3's contract: it is not enough
    for the DB row to be correct -- the *rendered* Xray candidate config
    that XrayFileProvider.render() would ship must also key
    routing.rules[].user on the routing_principal, not on
    accounting_user_id/username. This renders the real
    GatewayRouteBinding row (persisted by the production call path above)
    into a DesiredRoutingState exactly the way
    SqlAlchemyProvisioningState.desired_routing_state() does, and confirms
    the resulting CandidateConfig's rule uses the routing_principal as
    the `user` value."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="XRAYUSERKEY")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    accounting = MockAccountingProvider()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="bob"),
                object(),
                state,
                runs,
                order_state,
                "receipt-xray-user-key",
                providers=_registry(accounting=accounting),
            )
        finally:
            runs.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.SUCCEEDED
    routing_principal = accounting.users["bob"].routing_principal

    with Session(engine) as db:
        binding = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is not None
        current_endpoint = db.get(EgressEndpoint, binding.egress_id)
        assert current_endpoint is not None
        desired = DesiredRoutingState(
            user_routes={binding.gateway_principal: binding.outbound_tag},
            outbounds=(
                XrayOutboundDTO(
                    binding.outbound_tag,
                    current_endpoint.host,
                    current_endpoint.port,
                    current_endpoint.protocol,
                    current_endpoint.credential_secret_ref,
                ),
            ),
        )

    candidate = XrayFileProvider(
        runtime=None,  # type: ignore[arg-type]
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="resolver.example:443",
            reality_server_names=("resolver.example",),
        ),
    ).render(
        desired, _FakeCredentialResolver()
    )
    rules = candidate.content["routing"]["rules"]  # type: ignore[index]
    user_rule = next(rule for rule in rules if rule.get("user"))  # type: ignore[union-attr]

    assert user_rule["user"] == [routing_principal]
    assert user_rule["user"] != ["bob"]
    assert user_rule["outboundTag"] == binding.outbound_tag


def test_production_empty_routing_principal_fails_closed_before_apply_gateway(
    engine: Engine,
) -> None:
    """ADR-016 Decision 3's fail-closed contract: if the accounting
    provider returns an empty/whitespace-only routing_principal,
    CREATE_ACCOUNTING_USER must treat it as a failure -- disable the
    username, roll back phase-A DB state, mark the run FAILED at
    CREATE_ACCOUNTING_USER, never reach APPLY_GATEWAY, never acquire the
    named lock, and leave zero GatewayRouteBinding rows. No fallback to
    username or an egress tenant_id is permitted."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="EMPTYPRINCIPAL")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    class BlankPrincipalAccounting(MockAccountingProvider):
        def create_user(
            self, username: str, quota_bytes: int, expire_at: Any
        ) -> AccountUserDTO:
            return AccountUserDTO(
                username=username,
                quota_bytes=quota_bytes,
                expire_at=expire_at,
                routing_principal="   ",
            )

    accounting = BlankPrincipalAccounting()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(RuntimeError, match="empty or whitespace-only"):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="blank-principal"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-blank-principal",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()

    assert accounting.disabled_users == {"blank-principal"}

    with Session(engine) as verify_db:
        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    # The named lock was never acquired -- a fresh acquisition must
    # succeed immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_username_mismatch_fails_closed_before_apply_gateway(
    engine: Engine,
) -> None:
    """Same fail-closed contract as above, for the other half of ADR-016's
    required validation: create_user() returning a username that does not
    match request.username must also be treated as a CREATE_ACCOUNTING_USER
    failure, not silently trusted."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="USERNAMEMISMATCH")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    class MismatchedUsernameAccounting(MockAccountingProvider):
        def create_user(
            self, username: str, quota_bytes: int, expire_at: Any
        ) -> AccountUserDTO:
            return AccountUserDTO(
                username="someone-else",
                quota_bytes=quota_bytes,
                expire_at=expire_at,
                routing_principal="mock-routing.someone-else",
            )

    accounting = MismatchedUsernameAccounting()
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(RuntimeError, match="contract violation"):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="username-mismatch"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-username-mismatch",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()

    assert accounting.disabled_users == {"username-mismatch"}

    with Session(engine) as verify_db:
        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# ADR-018 (independent review of TASK-T16 Phase 2B7, PR #64 Major 1):
# CREATE_ACCOUNTING_USER compensation ownership. These four tests drive the
# *real* MarzbanAccountingProvider (httpx.MockTransport, no real network)
# through the full production call path (confirm_payment_and_provision ->
# provision_prepare -> _SqlAlchemyProvisionRuns/_OrderProvisioningState
# against real MySQL) -- not a hand-rolled fake -- so a regression in the
# real Marzban-adapter-to-domain wiring fails these tests too.
# ---------------------------------------------------------------------------

_MARZBAN_BASE_URL = "https://marzban.internal.invalid"
_MARZBAN_INBOUNDS_JSON = '{"vless": ["VLESS TCP REALITY"]}'


def _marzban_provider(handler: Any) -> MarzbanAccountingProvider:
    return MarzbanAccountingProvider(
        base_url=_MARZBAN_BASE_URL,
        admin_username="admin",
        admin_password="s3cret-admin-password",
        default_protocol="vless",
        default_inbounds_json=_MARZBAN_INBOUNDS_JSON,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_production_409_create_conflict_never_disables_an_unrelated_user(
    engine: Engine,
) -> None:
    """ADR-018 scenario A: a 409 (exact-source proven pre-commit
    rejection) must never trigger disable_user() -- request.username may
    be a pre-existing, unrelated Marzban account. Must roll back phase-A
    DB state, mark the run FAILED, never reach APPLY_GATEWAY, acquire
    zero named locks, and leave zero GatewayRouteBinding rows."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018A")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"})
        return httpx.Response(409, json={"detail": "User already exists"})

    accounting = _marzban_provider(handler)
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(AccountingCreateUserError):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="adr018a-user"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-adr018a",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()
            accounting.close()

    # No PUT (disable) and no DELETE were ever issued for this username.
    assert all(r.method != "DELETE" for r in requests)
    assert all(
        not (r.method == "PUT" and r.url.path == "/api/user/adr018a-user") for r in requests
    )

    with Session(engine) as verify_db:
        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    # Named lock was never acquired -- a fresh acquisition succeeds immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_422_validation_rejection_never_disables_or_pends_manual(
    engine: Engine,
) -> None:
    """ADR-018 round 4: a 422 (FastAPI's own request-validation response
    for add_user()'s plain Pydantic-model `new_user: UserCreate` body
    parameter -- raised before the route function body, and therefore
    before crud.create_user()'s db.commit(), ever runs) is exact-source
    proven NO_SIDE_EFFECT, exactly like 400/409. Must never be
    (mis)classified AMBIGUOUS -- which would wrongly route a definite
    pre-commit rejection into PENDING_MANUAL and keep phase-A's DB
    progress instead of rolling it back. Must roll back phase-A DB state,
    mark the run FAILED, never reach APPLY_GATEWAY, acquire zero named
    locks, and leave zero GatewayRouteBinding rows."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018F")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"})
        return httpx.Response(
            422, json={"detail": [{"loc": ["body", "username"], "msg": "field required"}]}
        )

    accounting = _marzban_provider(handler)
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(AccountingCreateUserError) as excinfo:
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="adr018f-user"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-adr018f",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()
            accounting.close()

    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT

    post_attempts = [r for r in requests if r.method == "POST" and r.url.path == "/api/user"]
    assert len(post_attempts) == 1
    assert all(r.method != "PUT" for r in requests)
    assert all(r.method != "DELETE" for r in requests)
    assert all(r.method != "GET" for r in requests)

    with Session(engine) as verify_db:
        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    # Named lock was never acquired -- a fresh acquisition succeeds immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_ambiguous_transport_failure_requires_manual_review(
    engine: Engine,
) -> None:
    """ADR-018 scenario B: a transport failure after the create POST was
    dispatched must never be blindly retried or blindly compensated --
    the request may or may not have created a real Marzban user. Must
    reach PENDING_MANUAL (never a plain FAILED, never a plain success),
    never touch the named lock, and leave zero GatewayRouteBinding rows."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018B")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"})
        raise httpx.ConnectTimeout("simulated ambiguous transport failure", request=request)

    accounting = _marzban_provider(handler)
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="adr018b-user"),
                object(),
                state,
                runs,
                order_state,
                "receipt-adr018b",
                providers=_registry(accounting=accounting),
            )
        finally:
            runs.close()
            accounting.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.pending_manual_error is not None
    assert "ambiguous" in outcome.pending_manual_error

    post_attempts = [r for r in requests if r.method == "POST" and r.url.path == "/api/user"]
    assert len(post_attempts) == 1
    assert all(r.method != "PUT" for r in requests)
    assert all(r.method != "DELETE" for r in requests)

    with Session(engine) as verify_db:
        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.PENDING

        # ADR-018 Major 2 (round 2): the business-facing reason must
        # correctly describe an ambiguous accounting create -- never the
        # CREATE_TENANT-era hardcoded "external tenant creation" message,
        # which would misdirect manual review to the wrong subsystem.
        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.provision_error is not None
        assert "external tenant creation" not in persisted_subscription.provision_error
        assert "accounting" in persisted_subscription.provision_error
        assert "ambiguous" in persisted_subscription.provision_error

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_created_user_with_malformed_response_is_disabled_and_failed(
    engine: Engine,
) -> None:
    """ADR-018 scenario C: HTTP 200 proves a user was created; a
    postcondition failure (missing routing_principal) in that same
    response is CREATED -- must attempt exactly one disable PUT (never
    DELETE), and once that disable succeeds, roll back and reach FAILED,
    never APPLY_GATEWAY."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018C")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"})
        if request.method == "POST" and request.url.path == "/api/user":
            return httpx.Response(
                200,
                json={
                    "username": "adr018c-user",
                    "status": "active",
                    "data_limit": 0,
                    "expire": 0,
                    # routing_principal deliberately omitted.
                },
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"status": "disabled"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    accounting = _marzban_provider(handler)
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(AccountingCreateUserError, match="routing_principal"):
                confirm_payment_and_provision(
                    _command(order_id),
                    _request(order_id, customer_id, username="adr018c-user"),
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-adr018c",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()
            accounting.close()

    disable_puts = [
        r for r in requests if r.method == "PUT" and r.url.path == "/api/user/adr018c-user"
    ]
    assert len(disable_puts) == 1
    assert all(r.method != "DELETE" for r in requests)

    with Session(engine) as verify_db:
        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_created_user_disable_failure_requires_manual_review(
    engine: Engine,
) -> None:
    """ADR-018 scenario D: HTTP 200 proves a user was created; the
    postcondition then fails, and disabling that definitely-owned user
    also fails. This must never be reported as a plain, fully-compensated
    FAILED (the external account may still be enabled) and must never
    continue into APPLY_GATEWAY -- it must reach PENDING_MANUAL."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018D")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        customer_id = subscription.customer_id

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"})
        if request.method == "POST" and request.url.path == "/api/user":
            return httpx.Response(
                200,
                json={
                    "username": "adr018d-user",
                    "status": "active",
                    "data_limit": 0,
                    "expire": 0,
                    # routing_principal deliberately omitted.
                },
            )
        if request.method == "PUT":
            return httpx.Response(500, text="internal error")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    accounting = _marzban_provider(handler)
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                _command(order_id),
                _request(order_id, customer_id, username="adr018d-user"),
                object(),
                state,
                runs,
                order_state,
                "receipt-adr018d",
                providers=_registry(accounting=accounting),
            )
        finally:
            runs.close()
            accounting.close()

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.pending_manual_error is not None
    assert "disabling" in outcome.pending_manual_error

    disable_attempts = [
        r for r in requests if r.method == "PUT" and r.url.path == "/api/user/adr018d-user"
    ]
    assert len(disable_attempts) == 1

    with Session(engine) as verify_db:
        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.PENDING

        # ADR-018 Major 2 (round 2): the business-facing reason must
        # correctly describe a failed compensation (disable) attempt --
        # never the CREATE_TENANT-era hardcoded "external tenant
        # creation" message, and never a raw provider exception message.
        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.provision_error is not None
        assert "external tenant creation" not in persisted_subscription.provision_error
        assert "compensation" in persisted_subscription.provision_error

    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass


def test_production_pre_epoch_expire_never_dispatches_or_disables(engine: Engine) -> None:
    """ADR-018 round 2 (Major 1): a pre-dispatch request-construction
    failure (a timezone-aware expire_at whose epoch is <= 0) happens
    entirely inside create_user() before any HTTP request is ever sent --
    it must classify NO_SIDE_EFFECT and never disable request.username.
    The handler asserts on any request at all to prove zero dispatch."""
    with Session(engine) as seed_db:
        order, subscription, endpoint = _seed(seed_db, suffix="ADR018E")
        seed_db.commit()
        order_id = order.id
        subscription_id = subscription.id
        endpoint_id = endpoint.id
        customer_id = subscription.customer_id

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"unexpected request: {request.method} {request.url.path} -- "
            "a pre-dispatch validation failure must never reach the network"
        )

    accounting = _marzban_provider(handler)
    pre_epoch_request = ProvisionRequest(
        order_id=str(order_id),
        customer_id=str(customer_id),
        username="adr018e-user",
        quota_gb=Decimal("10"),
        thread_limit=10,
        expire_at=datetime(1969, 1, 1, tzinfo=UTC),
        subscription_domain="subs.example.invalid",
    )
    with Session(engine) as db:
        state = _OrderProvisioningState(db, order_id)
        order_state = _SqlAlchemyOrderState(db)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            with pytest.raises(AccountingCreateUserError) as excinfo:
                confirm_payment_and_provision(
                    _command(order_id),
                    pre_epoch_request,
                    object(),
                    state,
                    runs,
                    order_state,
                    "receipt-adr018e",
                    providers=_registry(accounting=accounting),
                )
        finally:
            runs.close()
            accounting.close()

    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT

    with Session(engine) as verify_db:
        rolled_back_endpoint = verify_db.get(EgressEndpoint, endpoint_id)
        assert rolled_back_endpoint is not None
        assert rolled_back_endpoint.current_count == 0

        gateway_binding_count = verify_db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        assert gateway_binding_count == 0

        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status is SubscriptionStatus.PROVISION_FAILED

        job = verify_db.scalar(select(Job).where(Job.dedupe_key == f"provision:{order_id}"))
        assert job is not None
        assert job.status is JobStatus.FAILED

    # Named lock was never acquired -- a fresh acquisition succeeds immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass

