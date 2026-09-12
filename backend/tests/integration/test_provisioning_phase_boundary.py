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
        def create_user(self, username: str, quota_bytes: int, expire_at: Any) -> None:
            super().create_user(username, quota_bytes, expire_at)
            armed["value"] = True

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
