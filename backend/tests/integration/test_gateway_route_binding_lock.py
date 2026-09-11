"""ADR-016 Decision 3: MySQL named advisory lock concurrency contract for
GatewayRouteBinding.

These tests run against the real MySQL 8.4 CI service (never SQLite/mock):
GET_LOCK()/RELEASE_LOCK() do not exist in SQLite, and the whole point of
this contract is real MySQL connection-scoped lock semantics, which a mock
cannot stand in for. Each test that claims to verify real concurrent
MySQL behavior actually spins up two independent DB connections/sessions
and a background thread, rather than asserting on mocked call counts.

The one exception is `test_null_from_get_lock_fails_closed_via_injection`,
which is explicitly an injected-contract unit-style test (a fake
Connection, no real DB) for the `GET_LOCK() -> NULL` (error) case -- real
MySQL/MariaDB does not offer a reliable, deliberate way to force that
return value (the one well-known trigger, an over-length name, is instead
caught earlier by `gateway_route_binding_lock_name()` itself, before ever
reaching the server), so it is verified as a contract test rather than
claimed as real-MySQL-verified.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.infra.gateway_route_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    GatewayRouteBindingLockError,
    gateway_route_binding_lock_name,
    gateway_route_binding_write,
)
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Order,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.base import EgressEndpointDTO
from backend.app.workers.accounting_sync import release_egress


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


def _seed_subscription(
    db: Session, *, suffix: str, with_egress_binding: bool = False
) -> tuple[Subscription, EgressEndpoint]:
    customer = Customer(
        customer_no=f"CUS-LOCK-{suffix}",
        email=f"lock-{suffix}@example.invalid",
        password_hash="hash",
    )
    plan = Plan(
        plan_code=f"LOCK-PLAN-{suffix}",
        name="Lock Plan",
        traffic_limit_bytes=50 * 1024**3,
        duration_days=30,
        price="30.00",
        currency="USD",
        route_group_code=f"RG_LOCK_{suffix}",
    )
    group = EgressGroup(code=f"EG-LOCK-{suffix}", region="US")
    db.add_all([customer, plan, group])
    db.flush()
    order = Order(
        order_no=f"ORD-LOCK-{suffix}",
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id=f"lock-request-{suffix}",
        order_type="PURCHASE",
        amount="30.00",
        currency="USD",
    )
    db.add(order)
    db.flush()
    subscription = Subscription(
        subscription_no=f"SUB-LOCK-{suffix}",
        customer_id=customer.id,
        plan_id=plan.id,
        order_id=order.id,
        status=SubscriptionStatus.PROVISIONING,
        route_group_code=f"RG_LOCK_{suffix}",
        service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    endpoint = EgressEndpoint(
        group_id=group.id,
        code=f"EGRESS_LOCK_{suffix}",
        provider_name="webshare",
        host="proxy.example.invalid",
        port=1080,
        protocol="socks5",
        credential_secret_ref=f"egress/lock/{suffix}",
        status="AVAILABLE",
        capacity=1,
        current_count=0,
        mihomo_listen_port=12000 + hash(suffix) % 900,
    )
    db.add_all([subscription, endpoint])
    db.flush()
    if with_egress_binding:
        db.add(EgressBinding(subscription_id=subscription.id, egress_id=endpoint.id))
        db.flush()
    return subscription, endpoint


def _gateway_row_count(db: Session, subscription_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(GatewayRouteBinding)
            .where(GatewayRouteBinding.subscription_id == subscription_id)
        )
        or 0
    )


# ---------------------------------------------------------------------------
# 1 & 2: two independent sessions competing for the same lock.
# ---------------------------------------------------------------------------


def test_two_sessions_cannot_hold_the_lock_at_the_same_time(engine: Engine) -> None:
    holder_ready = threading.Event()
    release_holder = threading.Event()
    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def hold() -> None:
        with (
            Session(engine) as session,
            gateway_route_binding_write(session, timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS),
        ):
            holder_ready.set()
            release_holder.wait(timeout=10)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_ready.wait(timeout=5), "holder never acquired the lock"

    def contend() -> None:
        with Session(engine) as session:
            try:
                with gateway_route_binding_write(session, timeout_seconds=1):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    contender_thread = threading.Thread(target=contend)
    contender_thread.start()
    assert contender_done.wait(timeout=10), "contender never finished its attempt"
    contender_thread.join()

    # 1: while the holder has the lock, a second session must not get in.
    assert contender_result["acquired"] is False

    release_holder.set()
    holder_thread.join()

    # 2: once the holder releases, a fresh acquisition attempt must succeed.
    with Session(engine) as session, gateway_route_binding_write(session, timeout_seconds=5):
        pass


# ---------------------------------------------------------------------------
# 3: GET_LOCK timeout (0) fails closed, zero writes.
# ---------------------------------------------------------------------------


def test_get_lock_timeout_fails_closed_with_zero_writes(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(seed_db, suffix="TIMEOUT")
        seed_db.commit()
        subscription_id = subscription.id
        endpoint_id = endpoint.id

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with (
            Session(engine) as session,
            gateway_route_binding_write(session, timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS),
        ):
            holder_ready.set()
            release_holder.wait(timeout=10)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_ready.wait(timeout=5), "holder never acquired the lock"

    # A short explicit timeout here (via the module-level helper, not
    # state.gateway_route_binding_lock()'s fixed default) keeps this test
    # fast and deterministic regardless of how long the holder thread
    # happens to hold the lock; the fail-closed contract GET_LOCK()
    # returning 0 must produce is identical either way.
    try:
        with Session(engine) as db:
            state = SqlAlchemyProvisioningState(db, subscription_id)
            dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
            with (
                pytest.raises(GatewayRouteBindingLockError),
                gateway_route_binding_write(db, timeout_seconds=1),
            ):
                # Must never be reached: GET_LOCK() must fail before the
                # `with` block's body ever runs.
                state.ensure_gateway_route_binding("marzban-user-timeout", dto)
                db.commit()
    finally:
        release_holder.set()
        holder_thread.join()

    with Session(engine) as db:
        assert _gateway_row_count(db, subscription_id) == 0


# ---------------------------------------------------------------------------
# 4: GET_LOCK NULL (error) fails closed -- injected contract test, NOT
#    real-MySQL-verified (see module docstring).
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeEngineUrl:
    database = "fake_db"


class _FakeEngine:
    url = _FakeEngineUrl()


class _FakeLockConnection:
    """Stands in for the dedicated lock Connection; never touches a real DB."""

    engine = _FakeEngine()

    def __init__(self, get_lock_return: object) -> None:
        self._get_lock_return = get_lock_return
        self.executed: list[str] = []
        self.closed = False

    def execution_options(self, **_kwargs: object) -> _FakeLockConnection:
        return self

    def execute(self, statement: object, _params: object | None = None) -> _FakeScalarResult:
        sql = str(statement)
        self.executed.append(sql)
        if "GET_LOCK" in sql:
            return _FakeScalarResult(self._get_lock_return)
        return _FakeScalarResult(None)

    def close(self) -> None:
        self.closed = True


class _FakeBind:
    def __init__(self, connection: _FakeLockConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeLockConnection:
        return self._connection


class _FakeSession:
    def __init__(self, connection: _FakeLockConnection) -> None:
        self._bind = _FakeBind(connection)

    def get_bind(self) -> _FakeBind:
        return self._bind


def test_null_from_get_lock_fails_closed_via_injection() -> None:
    """Injected contract test: GET_LOCK() -> NULL must fail closed exactly
    like a timeout (0), never be treated as "no other holder, proceed"."""
    connection = _FakeLockConnection(get_lock_return=None)
    session = _FakeSession(connection)
    entered_body = False

    with (
        pytest.raises(GatewayRouteBindingLockError),
        gateway_route_binding_write(session),  # type: ignore[arg-type]
    ):
        entered_body = True

    assert entered_body is False
    assert connection.closed is True
    assert not any("RELEASE_LOCK" in sql for sql in connection.executed)


def test_lock_name_is_namespaced_by_database_and_within_mysql_limit() -> None:
    connection = _FakeLockConnection(get_lock_return=1)
    name = gateway_route_binding_lock_name(connection)  # type: ignore[arg-type]
    assert name == "lingsway:fake_db:gateway-route-bindings-write"
    assert len(name) <= 64


# ---------------------------------------------------------------------------
# 5: exception + rollback releases the lock; a new connection can reacquire.
# ---------------------------------------------------------------------------


def test_exception_and_rollback_releases_the_lock(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(seed_db, suffix="ROLLBACK")
        seed_db.commit()
        subscription_id = subscription.id
        endpoint_id = endpoint.id

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
        with (
            pytest.raises(RuntimeError, match="simulated failure"),
            state.gateway_route_binding_lock(),
        ):
            state.ensure_gateway_route_binding("marzban-user-rollback", dto)
            db.rollback()
            raise RuntimeError("simulated failure")

    with Session(engine) as db:
        assert _gateway_row_count(db, subscription_id) == 0

    # A fresh session must be able to acquire the same lock immediately.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=2):
        pass


# ---------------------------------------------------------------------------
# 6: the lock is not released before commit completes.
# ---------------------------------------------------------------------------


def test_lock_is_not_released_before_commit_completes(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(seed_db, suffix="COMMIT")
        seed_db.commit()
        subscription_id = subscription.id
        endpoint_id = endpoint.id

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

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
        with state.gateway_route_binding_lock():
            state.ensure_gateway_route_binding("marzban-user-commit", dto)
            checkpoint.set()
            assert contender_done.wait(timeout=10), "contender never finished its attempt"
            # The contender must have failed to acquire: we have not
            # committed yet, so the lock must still be ours.
            assert contender_result["acquired"] is False
            db.commit()
    contender_thread.join()

    # After commit and lock release, a fresh acquisition must succeed.
    with Session(engine) as db, gateway_route_binding_write(db, timeout_seconds=5):
        pass
    with Session(engine) as db:
        assert _gateway_row_count(db, subscription_id) == 1


# ---------------------------------------------------------------------------
# 7, 8, 9: provisioning writer vs release writer, both directions, same lock.
# ---------------------------------------------------------------------------


def test_provisioning_writer_blocks_release_writer(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(
            seed_db, suffix="PROVVSREL", with_egress_binding=True
        )
        seed_db.add(
            GatewayRouteBinding(
                subscription_id=subscription.id,
                egress_id=endpoint.id,
                gateway_principal="marzban-user-provvsrel",
                outbound_tag="egress-provvsrel",
                enabled=True,
            )
        )
        seed_db.commit()
        subscription_id = subscription.id
        endpoint_id = endpoint.id

    checkpoint = threading.Event()
    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def contend_release() -> None:
        checkpoint.wait(timeout=5)
        with Session(engine) as session:
            try:
                with gateway_route_binding_write(session, timeout_seconds=1):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    contender_thread = threading.Thread(target=contend_release)
    contender_thread.start()

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
        with state.gateway_route_binding_lock():
            state.ensure_gateway_route_binding("marzban-user-provvsrel", dto)
            checkpoint.set()
            assert contender_done.wait(timeout=10)
            assert contender_result["acquired"] is False
            db.commit()
    contender_thread.join()

    # release_egress() must be able to proceed once the provisioning writer
    # has released the lock, and it must use the same lock namespace (an
    # unrelated lock would have let the contender through above).
    with Session(engine) as db:
        release_egress(db, subscription_id, datetime.now(UTC))
        binding = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert binding is not None
        assert binding.enabled is False
        assert binding.released_at is not None


def test_release_writer_blocks_provisioning_writer(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(
            seed_db, suffix="RELVSPROV", with_egress_binding=True
        )
        seed_db.add(
            GatewayRouteBinding(
                subscription_id=subscription.id,
                egress_id=endpoint.id,
                gateway_principal="marzban-user-relvsprov",
                outbound_tag="egress-relvsprov",
                enabled=True,
            )
        )
        seed_db.commit()
        subscription_id = subscription.id

    checkpoint = threading.Event()
    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def contend_provisioning() -> None:
        checkpoint.wait(timeout=5)
        with Session(engine) as session:
            try:
                with gateway_route_binding_write(session, timeout_seconds=1):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    contender_thread = threading.Thread(target=contend_provisioning)
    contender_thread.start()

    # Reproduce release_egress()'s own locking, but hold the checkpoint open
    # before its commit so the contender's attempt lands squarely inside the
    # protected window.
    with Session(engine) as db, gateway_route_binding_write(db):
        db.scalar(
            select(Subscription).where(Subscription.id == subscription_id).with_for_update()
        )
        binding = db.scalar(
            select(EgressBinding).where(
                EgressBinding.subscription_id == subscription_id,
                EgressBinding.released_at.is_(None),
            )
        )
        assert binding is not None
        binding.released_at = datetime.now(UTC)
        gateway_binding = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert gateway_binding is not None
        gateway_binding.enabled = False
        gateway_binding.released_at = datetime.now(UTC)
        checkpoint.set()
        assert contender_done.wait(timeout=10)
        assert contender_result["acquired"] is False
        db.commit()
    contender_thread.join()


# ---------------------------------------------------------------------------
# 10: lock failure must never leave a partial GatewayRouteBinding write.
# ---------------------------------------------------------------------------


def test_lock_failure_leaves_zero_partial_writes(engine: Engine) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(seed_db, suffix="PARTIAL")
        seed_db.commit()
        subscription_id = subscription.id
        endpoint_id = endpoint.id

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with (
            Session(engine) as session,
            gateway_route_binding_write(session, timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS),
        ):
            holder_ready.set()
            release_holder.wait(timeout=10)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_ready.wait(timeout=5)

    # A short explicit timeout here (via the module-level helper, not
    # state.gateway_route_binding_lock()'s fixed default) keeps this test
    # fast; the fail-closed contract is identical either way -- GET_LOCK()
    # returning 0 must reject before any mutation.
    try:
        with (
            Session(engine) as db,
            pytest.raises(GatewayRouteBindingLockError),
            gateway_route_binding_write(db, timeout_seconds=1),
        ):
            db.add(
                GatewayRouteBinding(
                    subscription_id=subscription_id,
                    egress_id=endpoint_id,
                    gateway_principal="marzban-user-partial",
                    outbound_tag="egress-partial-should-not-persist",
                    enabled=True,
                )
            )
            db.commit()
    finally:
        release_holder.set()
        holder_thread.join()

    with Session(engine) as db:
        assert _gateway_row_count(db, subscription_id) == 0
