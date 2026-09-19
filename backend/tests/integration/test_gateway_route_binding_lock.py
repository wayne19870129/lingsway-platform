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
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

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
        # Real sqlalchemy.orm.Session always has this plain dict attribute;
        # gateway_route_binding_write() uses it to record ADR-017's
        # lock-held marker (see SESSION_INFO_LOCK_HELD_KEY).
        self.info: dict[str, object] = {}

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


class _RaisingLockConnection:
    """Stands in for the dedicated lock Connection; never touches a real DB.

    Unlike :class:`_FakeLockConnection` (which returns 0/NULL from
    ``GET_LOCK()``), this simulates a generic DBAPI/SQLAlchemy exception
    *raised while executing* the ``GET_LOCK()`` statement itself -- e.g. a
    dropped connection or pool exhaustion -- which is a distinct failure
    mode from the two documented ``GET_LOCK()`` return values.
    """

    engine = _FakeEngine()

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.closed = False
        self.invalidated = False

    def execution_options(self, **_kwargs: object) -> _RaisingLockConnection:
        return self

    def execute(self, statement: object, _params: object | None = None) -> _FakeScalarResult:
        sql = str(statement)
        if "GET_LOCK" in sql:
            raise self._exc
        return _FakeScalarResult(None)

    def invalidate(self) -> None:
        self.invalidated = True

    def close(self) -> None:
        self.closed = True


def test_generic_exception_during_get_lock_is_normalized_to_lock_error() -> None:
    """Major 1 (Work review round 2): a generic DBAPI/SQLAlchemy exception
    raised while executing ``GET_LOCK()`` -- not just its 0/NULL return
    values -- must also be normalized into ``GatewayRouteBindingLockError``,
    with the original exception chained via ``from``, so callers that only
    catch ``GatewayRouteBindingLockError`` to run their fail-closed
    compensation (``services.confirm_payment_and_provision``) still see it
    for this failure mode too. The public message must not embed the raw
    DBAPI exception text -- only the chained cause carries it."""
    original = RuntimeError("simulated DBAPI: connection reset by peer")
    connection = _RaisingLockConnection(original)
    session = _FakeSession(connection)
    entered_body = False

    with (
        pytest.raises(GatewayRouteBindingLockError) as excinfo,
        gateway_route_binding_write(session),  # type: ignore[arg-type]
    ):
        entered_body = True

    assert entered_body is False
    assert connection.closed is True
    assert excinfo.value.__cause__ is original
    assert "connection reset by peer" not in str(excinfo.value)


def test_generic_exception_during_get_lock_invalidates_the_ambiguous_connection() -> None:
    """Major 1 (Work review round 2): the server may have already granted
    the lock before a generic GET_LOCK() exception happened in transit --
    this connection's lock state is unknown, so it must be invalidated
    (not just closed) rather than risk returning a connection the server
    still considers the lock's holder back into the pool."""
    connection = _RaisingLockConnection(RuntimeError("simulated transport failure"))
    session = _FakeSession(connection)

    with (
        pytest.raises(GatewayRouteBindingLockError),
        gateway_route_binding_write(session),  # type: ignore[arg-type]
    ):
        pass

    assert connection.invalidated is True
    assert connection.closed is True


def test_get_lock_return_value_failures_do_not_invalidate_the_connection() -> None:
    """Control case for the test above: GET_LOCK() returning 0/NULL is an
    unambiguous, server-confirmed "no lock granted" outcome -- the
    connection is known-clean and must not be needlessly invalidated."""
    connection = _FakeLockConnection(get_lock_return=0)
    session = _FakeSession(connection)

    with (
        pytest.raises(GatewayRouteBindingLockError),
        gateway_route_binding_write(session),  # type: ignore[arg-type]
    ):
        pass

    assert connection.closed is True


def test_ambiguous_failure_after_real_get_lock_success_does_not_leak_the_lock(
    engine: Engine,
) -> None:
    """Major 1 (Work review round 2), real MySQL: simulate the actual
    ambiguous scenario the injected unit tests above only assert the
    *reaction* to -- the dedicated connection genuinely acquires the real
    named lock on the server, and only *then* does the code path raise, as
    if the response confirming that success had been lost in transit. The
    fix must not just raise the right exception type: it must actually
    invalidate the connection so MySQL does not keep considering it the
    lock's holder. Proven by using a completely independent, fresh
    connection afterward to acquire the same lock immediately -- if the
    ambiguous connection had merely been closed and returned to the pool
    instead of invalidated, MySQL would still hold it as the owner (named
    locks are not released by an ordinary close/rollback) and this second
    acquisition would time out instead."""
    import backend.app.infra.gateway_route_lock as lock_module

    real_get_lock = lock_module._get_lock

    def flaky_get_lock(connection: Any, name: str, timeout_seconds: int) -> None:
        # Actually acquires the real named lock on the real dedicated
        # connection first -- then fails as if the confirmation of that
        # success never made it back to the caller.
        real_get_lock(connection, name, timeout_seconds)
        raise RuntimeError("simulated: response lost after the server granted the lock")

    with (
        mock.patch.object(lock_module, "_get_lock", side_effect=flaky_get_lock),
        Session(engine) as db,
        pytest.raises(GatewayRouteBindingLockError),
        gateway_route_binding_write(db, timeout_seconds=5),
    ):
        pass

    # A fresh, completely independent connection must be able to acquire
    # the same named lock immediately -- proving the ambiguous connection
    # was invalidated (and thus terminated, releasing the server-side
    # lock) rather than pooled while still perceived as the holder.
    with Session(engine) as verify_db, gateway_route_binding_write(verify_db, timeout_seconds=2):
        pass


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
    contender_errors: list[BaseException] = []

    def contend_release() -> None:
        checkpoint.wait(timeout=5)
        # The real production writer, not a stand-in for it: if
        # release_egress() ever stopped acquiring the named lock, this call
        # would return almost immediately instead of blocking below, and
        # the "still not done" assertion in the main thread would fail.
        with Session(engine) as session:
            try:
                release_egress(session, subscription_id, datetime.now(UTC))
            except BaseException as exc:  # noqa: BLE001 - captured for assertion, not swallowed
                contender_errors.append(exc)
        contender_done.set()

    contender_thread = threading.Thread(target=contend_release)
    contender_thread.start()

    with Session(engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
        with state.gateway_route_binding_lock():
            # The checkpoint fires *before* any GatewayRouteBinding mutation
            # or flush happens -- deliberately, so no InnoDB row lock exists
            # yet. If it fired after ensure_gateway_route_binding()'s own
            # flush instead, release_egress()'s with_for_update() SELECT on
            # that same row would block on the row lock alone, and this
            # test would keep passing even if the *named* lock wiring were
            # removed from either writer -- it would no longer be testing
            # the thing ADR-016 actually requires.
            checkpoint.set()
            # release_egress() is blocked on its own real GET_LOCK() call
            # (default timeout) right now, purely by the named lock -- it
            # must not have finished yet.
            assert not contender_done.wait(timeout=1)
            state.ensure_gateway_route_binding("marzban-user-provvsrel", dto)
            db.commit()
    contender_thread.join(timeout=15)

    # release_egress() must have been able to proceed once the provisioning
    # writer released the lock, and it did so through the same lock
    # namespace (an unrelated lock would have let it through immediately,
    # above, instead of blocking).
    assert contender_done.is_set(), "release_egress() never completed"
    assert contender_errors == []
    with Session(engine) as db:
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
        endpoint_id = endpoint.id

    # release_egress() has no caller-visible pause point, so proving a
    # concurrent contender is genuinely blocked while it holds the lock
    # (rather than just "happened to run later") needs a deterministic
    # synchronization point. Patch the *module-level name*
    # accounting_sync.gateway_route_binding_write resolves at call time --
    # not the implementation itself, which still runs for real -- with a
    # thin wrapper that pauses right after the real GET_LOCK() succeeds and
    # before release_egress()'s own body (its mutation and commit) runs.
    # If release_egress() ever stopped calling gateway_route_binding_write()
    # at all, this patched name would simply never be invoked, `ready`
    # would never be set, and this test would fail on the `ready.wait()`
    # assertion below -- so this also directly guards against that writer
    # silently losing its lock wiring.
    ready = threading.Event()
    proceed = threading.Event()

    @contextmanager
    def paused_lock(session: Session, **kwargs: object) -> Iterator[None]:
        with gateway_route_binding_write(session, **kwargs):  # type: ignore[arg-type]
            ready.set()
            assert proceed.wait(timeout=15), "test harness never signalled proceed"
            yield

    holder_errors: list[BaseException] = []

    def hold_release() -> None:
        with (
            mock.patch(
                "backend.app.workers.accounting_sync.gateway_route_binding_write",
                paused_lock,
            ),
            Session(engine) as session,
        ):
            try:
                release_egress(session, subscription_id, datetime.now(UTC))
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                holder_errors.append(exc)

    holder_thread = threading.Thread(target=hold_release)
    holder_thread.start()
    assert ready.wait(timeout=5), "release_egress() never acquired the named lock"

    contender_done = threading.Event()
    contender_errors: list[BaseException] = []

    def contend_provisioning() -> None:
        # The real production provisioning writer, not a stand-in: if
        # ensure_gateway_route_binding()/gateway_route_binding_lock() ever
        # stopped acquiring the same named lock, this would succeed
        # immediately instead of blocking, and the "still not done"
        # assertion below would fail.
        with Session(engine) as session:
            state = SqlAlchemyProvisioningState(session, subscription_id)
            dto = EgressEndpointDTO(str(endpoint_id), "proxy.example.invalid", 1080)
            try:
                with state.gateway_route_binding_lock():
                    state.ensure_gateway_route_binding("marzban-user-relvsprov-2", dto)
                    session.commit()
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                contender_errors.append(exc)
        contender_done.set()

    contender_thread = threading.Thread(target=contend_provisioning)
    contender_thread.start()

    # release_egress() is paused (lock held, nothing committed yet) --
    # the real provisioning writer must not have gotten in yet.
    assert not contender_done.wait(timeout=1)

    proceed.set()
    holder_thread.join(timeout=15)
    contender_thread.join(timeout=15)

    assert holder_errors == []
    assert contender_errors == []
    assert contender_done.is_set()


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


# ---------------------------------------------------------------------------
# Major 1 regression (Work review round 2): release_egress() must roll back
# -- and only release the lock after that rollback completes -- if it fails
# after mutating but before its own commit() confirms.
# ---------------------------------------------------------------------------


def test_release_egress_failure_after_mutation_rolls_back_before_releasing_lock(
    engine: Engine,
) -> None:
    with Session(engine) as seed_db:
        subscription, endpoint = _seed_subscription(
            seed_db, suffix="RELROLLBACK", with_egress_binding=True
        )
        seed_db.add(
            GatewayRouteBinding(
                subscription_id=subscription.id,
                egress_id=endpoint.id,
                gateway_principal="marzban-user-relrollback",
                outbound_tag="egress-relrollback",
                enabled=True,
            )
        )
        seed_db.commit()
        subscription_id = subscription.id

    contender_done = threading.Event()
    contender_result: dict[str, Any] = {}

    def contend() -> None:
        with Session(engine) as session:
            try:
                # Generous timeout: this must succeed once (and only once)
                # release_egress()'s own rollback + lock release completes,
                # not fail with a short timeout -- a short timeout here
                # would only prove "we didn't wait long enough", not that
                # the lock was actually held throughout.
                with gateway_route_binding_write(session, timeout_seconds=15):
                    contender_result["acquired"] = True
            except GatewayRouteBindingLockError:
                contender_result["acquired"] = False
        contender_done.set()

    with Session(engine) as db:
        # Simulate release_egress() mutating GatewayRouteBinding/EgressBinding
        # successfully, and then its own db.commit() call failing --
        # "after mutation, before transaction finalization actually
        # confirms" per the review's scenario.
        def failing_commit() -> None:
            raise RuntimeError("simulated commit failure")

        db.commit = failing_commit  # type: ignore[method-assign]

        contender_thread = threading.Thread(target=contend)
        contender_thread.start()

        with pytest.raises(RuntimeError, match="simulated commit failure"):
            release_egress(db, subscription_id, datetime.now(UTC))

    contender_thread.join(timeout=20)

    # The contender could only have gotten in after release_egress()'s
    # `with gateway_route_binding_write(db):` block actually exited --
    # which only happens after its except-branch db.rollback() has already
    # run (that rollback is inside the same `with` block, so the lock's
    # `finally` cannot release before it, by construction).
    assert contender_done.is_set()
    assert contender_result["acquired"] is True

    with Session(engine) as verify_db:
        binding = verify_db.scalar(
            select(EgressBinding).where(EgressBinding.subscription_id == subscription_id)
        )
        assert binding is not None
        assert binding.released_at is None, "rollback must have undone the mutation"
        gateway_binding = verify_db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        assert gateway_binding is not None
        assert gateway_binding.enabled is True
        assert gateway_binding.released_at is None


# ---------------------------------------------------------------------------
# Minor: RELEASE_LOCK()'s own return value must be validated, not ignored.
# ---------------------------------------------------------------------------


class _RecordingLockConnection:
    """Stands in for the dedicated lock Connection; never touches a real DB."""

    engine = _FakeEngine()

    def __init__(self, get_lock_return: object, release_lock_return: object) -> None:
        self._get_lock_return = get_lock_return
        self._release_lock_return = release_lock_return
        self.invalidated = False
        self.closed = False

    def execution_options(self, **_kwargs: object) -> _RecordingLockConnection:
        return self

    def execute(self, statement: object, _params: object | None = None) -> _FakeScalarResult:
        sql = str(statement)
        if "GET_LOCK" in sql:
            return _FakeScalarResult(self._get_lock_return)
        if "RELEASE_LOCK" in sql:
            return _FakeScalarResult(self._release_lock_return)
        return _FakeScalarResult(None)

    def invalidate(self) -> None:
        self.invalidated = True

    def close(self) -> None:
        self.closed = True


def test_release_lock_not_confirmed_invalidates_the_connection() -> None:
    """Injected contract test: if RELEASE_LOCK() doesn't return 1, the
    connection's lock state is unverified and it must not be silently
    returned to the pool as healthy -- it must be invalidated instead."""
    connection = _RecordingLockConnection(get_lock_return=1, release_lock_return=0)
    session = _FakeSession(connection)

    with gateway_route_binding_write(session):  # type: ignore[arg-type]
        pass

    assert connection.invalidated is True
    assert connection.closed is True


def test_release_lock_success_does_not_invalidate_the_connection() -> None:
    """Control case for the above: a confirmed RELEASE_LOCK() (returns 1)
    must not needlessly discard a perfectly healthy connection."""
    connection = _RecordingLockConnection(get_lock_return=1, release_lock_return=1)
    session = _FakeSession(connection)

    with gateway_route_binding_write(session):  # type: ignore[arg-type]
        pass

    assert connection.invalidated is False
    assert connection.closed is True


# ---------------------------------------------------------------------------
# Major 1 regression (Work review round 3): release_egress()'s no-op path
# must not roll back a caller's already-pending, unrelated transaction.
# ---------------------------------------------------------------------------


def test_sync_subscription_usage_expiry_persists_when_no_active_egress_binding(
    engine: Engine,
) -> None:
    """The real sync_subscription_usage() -> release_egress() path, for a
    subscription with no active EgressBinding: release_egress()'s no-op
    branch must leave sync_subscription_usage()'s already-pending
    UsageSample/UsageDelta/UsagePeriod/subscription-expiry changes on the
    same Session intact for its own later db.commit() -- not roll them
    back just because there was nothing for the named lock to protect."""
    from backend.app.models import (
        Customer,
        Plan,
        Subscription,
        SubscriptionStatus,
        UsageDelta,
        UsagePeriod,
        UsagePeriodStatus,
        UsageSample,
    )
    from backend.app.providers.accounting.mock import MockAccountingProvider
    from backend.app.workers.accounting_sync import sync_subscription_usage

    now = datetime.now(UTC)
    with Session(engine) as db:
        customer = Customer(
            customer_no="CUS-LOCK-SYNCUSAGE",
            email="lock-syncusage@example.invalid",
            password_hash="hash",
        )
        plan = Plan(
            plan_code="LOCK-PLAN-SYNCUSAGE",
            name="Lock Plan",
            traffic_limit_bytes=50 * 1024**3,
            duration_days=30,
            price="30.00",
            currency="USD",
            route_group_code="RG_LOCK_SYNCUSAGE",
        )
        db.add_all([customer, plan])
        db.flush()
        order = Order(
            order_no="ORD-LOCK-SYNCUSAGE",
            customer_id=customer.id,
            plan_id=plan.id,
            client_request_id="lock-request-syncusage",
            order_type="PURCHASE",
            amount="30.00",
            currency="USD",
        )
        db.add(order)
        db.flush()
        subscription = Subscription(
            subscription_no="SUB-LOCK-SYNCUSAGE",
            customer_id=customer.id,
            plan_id=plan.id,
            order_id=order.id,
            accounting_user_id="marzban-user-syncusage",
            # GRACE + an already-past service_expire_at makes
            # apply_expiry_policy() advance straight to EXPIRED inside this
            # one sync_subscription_usage() call, which is what triggers
            # its release_egress() call -- for a subscription that, per
            # this test, has no active EgressBinding to release.
            status=SubscriptionStatus.GRACE,
            route_group_code="RG_LOCK_SYNCUSAGE",
            service_expire_at=now - timedelta(hours=1),
        )
        db.add(subscription)
        db.flush()
        period = UsagePeriod(
            subscription_id=subscription.id,
            order_id=order.id,
            period_start=now - timedelta(days=30),
            period_end=now,
            used_bytes=0,
            quota_bytes=50 * 1024**3,
            status=UsagePeriodStatus.ACTIVE,
        )
        db.add(period)
        db.flush()
        subscription.current_period_id = period.id
        db.commit()

        subscription_id = subscription.id
        period_id = period.id

        provider = MockAccountingProvider()
        provider.set_mock_usage("marzban-user-syncusage", 12345)

        # No EgressBinding/GatewayRouteBinding rows exist for this
        # subscription at all -- release_egress() must take its no-op path.
        delta_bytes = sync_subscription_usage(db, subscription, provider, now)
        db.commit()

    assert delta_bytes == 12345

    with Session(engine) as verify_db:
        persisted_subscription = verify_db.get(Subscription, subscription_id)
        assert persisted_subscription is not None
        assert persisted_subscription.status == SubscriptionStatus.EXPIRED
        assert persisted_subscription.last_usage_synced_at is not None

        persisted_period = verify_db.get(UsagePeriod, period_id)
        assert persisted_period is not None
        assert persisted_period.used_bytes == 12345

        sample = verify_db.scalar(
            select(UsageSample).where(UsageSample.subscription_id == subscription_id)
        )
        assert sample is not None
        assert sample.source_counter == 12345

        delta = verify_db.scalar(
            select(UsageDelta).where(UsageDelta.subscription_id == subscription_id)
        )
        assert delta is not None
        assert delta.delta_bytes == 12345

        assert _gateway_row_count(verify_db, subscription_id) == 0
