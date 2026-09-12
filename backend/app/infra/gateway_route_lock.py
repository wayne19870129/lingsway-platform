"""MySQL named advisory lock: the sole concurrency contract for
``GatewayRouteBinding`` mutations (ADR-016 Decision 3, step 4).

``GatewayRouteBinding`` has no index supporting the
``enabled = TRUE AND released_at IS NULL`` predicate its writers care about,
so a ``SELECT ... FOR UPDATE`` on that predicate cannot correctly serialize
concurrent writers -- there is no index range for InnoDB to lock. ADR-016
requires every current writer (``SqlAlchemyProvisioningState.
ensure_gateway_route_binding()`` and ``accounting_sync.release_egress()``,
and any future writer) to instead cooperate through one MySQL
``GET_LOCK()``/``RELEASE_LOCK()`` named lock before touching the table.

This is a cooperative, single-MySQL-server mechanism: it only coordinates
Lingsway code paths that go through :func:`gateway_route_binding_write`, not
a direct admin SQL session against the database, and it assumes a single
MySQL server / single write topology -- it provides no cross-server mutual
exclusion if the deployment ever moves to multi-primary MySQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

#: How long GET_LOCK() blocks waiting for the lock before giving up.
#:
#: This is a placeholder default, not a value derived from a measured
#: provisioning-saga latency budget: the provisioning writer currently
#: holds this lock across the mock egress/accounting/gateway/notify calls
#: exercised by tests, which are effectively instantaneous. Those providers
#: are mocks -- e.g. the real Webshare transport documents a 20s per-call
#: timeout before its own rate-limit/retry waits, well past this default.
#: TASK-T16 records that the lock's hold span and this timeout must be
#: re-evaluated once real (non-mock) provider wiring lands; until then,
#: treat 30s as an easily-replaced placeholder, not a validated budget.
DEFAULT_LOCK_TIMEOUT_SECONDS = 30

#: MySQL's documented GET_LOCK() name length limit (in bytes/characters).
_LOCK_NAME_MAX_LENGTH = 64

#: Key set on ``session.info`` (a plain dict SQLAlchemy attaches to every
#: ``Session`` and never touches itself) while that session's caller holds
#: the named lock via :func:`gateway_route_binding_write`. ADR-017's
#: production-bypass guard: ``SqlAlchemyProvisioningState.
#: ensure_gateway_route_binding()`` checks this before mutating, so a
#: caller that reaches it without having entered
#: ``gateway_route_binding_lock()`` first (e.g. a convenience API pointed
#: at a real state object outside the one sanctioned, lock-wrapped
#: production call path) fails closed instead of silently writing
#: unprotected. This is a real runtime check, not a naming convention or a
#: static grep for callers.
SESSION_INFO_LOCK_HELD_KEY = "gateway_route_binding_lock_held"


class GatewayRouteBindingLockError(RuntimeError):
    """The named lock could not be acquired; callers must fail closed.

    Raised when ``GET_LOCK()`` returns ``0`` (timeout) or ``NULL`` (error,
    e.g. an illegal lock name) -- both cases must be treated identically:
    zero ``GatewayRouteBinding`` mutation, no claim of having the lock.
    """


def gateway_route_binding_lock_name(bind: Connection | Engine) -> str:
    """Stable, namespaced, database-scoped lock name.

    Encoding the database name keeps staging/production (or any two
    databases sharing one MySQL server) from accidentally serializing
    against each other.
    """
    engine: Any = bind.engine if hasattr(bind, "engine") else bind
    database = engine.url.database
    if not database:
        raise GatewayRouteBindingLockError(
            "cannot derive a gateway route binding lock name: the database "
            "engine URL has no database name"
        )
    name = f"lingsway:{database}:gateway-route-bindings-write"
    if len(name) > _LOCK_NAME_MAX_LENGTH:
        raise GatewayRouteBindingLockError(
            "gateway route binding lock name exceeds MySQL's "
            f"{_LOCK_NAME_MAX_LENGTH}-character GET_LOCK() limit: {name!r}"
        )
    return name


def _get_lock(connection: Connection, name: str, timeout_seconds: int) -> None:
    result = connection.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": name, "timeout": timeout_seconds},
    ).scalar()
    if result != 1:
        # 0 = timed out waiting for another holder; NULL = error (e.g. an
        # illegal name). Both are fail-closed: the caller must not proceed
        # as if it were free to mutate GatewayRouteBinding.
        raise GatewayRouteBindingLockError(
            f"failed to acquire gateway route binding lock {name!r}: "
            f"GET_LOCK returned {result!r} (0=timeout, NULL=error)"
        )


def _release_lock(connection: Connection, name: str) -> None:
    try:
        result = connection.execute(
            text("SELECT RELEASE_LOCK(:name)"), {"name": name}
        ).scalar()
    except Exception:
        # We cannot confirm the lock was actually released on this
        # connection (e.g. the connection dropped mid-call). Discard it
        # rather than let a connection that may still be perceived by the
        # server as holding this lock go back into the pool as if nothing
        # happened; the caller still closes it right after this.
        connection.invalidate()
        raise
    if result != 1:
        # 1 = released by us, as expected. 0 = we did not hold the lock (it
        # was already released, timed out, or held by someone else) and
        # NULL = the named lock did not exist -- neither confirms a clean
        # release, so the connection's lock state is unverified: invalidate
        # it instead of silently returning what might be a compromised
        # connection to the pool for reuse.
        connection.invalidate()


@contextmanager
def gateway_route_binding_write(
    session: Session, *, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """The sole sanctioned way to mutate ``GatewayRouteBinding`` (ADR-016).

    Acquires the named lock on a **dedicated** MySQL connection -- separate
    from whatever connection(s) ``session`` itself checks out of the pool
    across its own transaction boundaries -- because MySQL named locks are
    connection-scoped, not transaction-scoped: ``GET_LOCK()``/
    ``RELEASE_LOCK()`` only have to run on the *same* connection as each
    other, and using a connection this helper owns end-to-end (opened here,
    closed here) is what actually guarantees that, rather than assuming an
    ORM ``Session`` keeps the same pooled DBAPI connection across multiple
    internal commit/rollback boundaries (it does not: each of a ``Session``'s
    transactions can be handed a different physical connection from the
    pool). The lock is held for the entire ``with`` block regardless of how
    many transactions ``session`` itself opens and closes inside it.

    The caller is responsible for performing its ``GatewayRouteBinding``
    mutation(s) *and* calling ``session.commit()``/``session.rollback()``
    for them inside this ``with`` block, in that order, before the block
    exits: MySQL does not release a ``GET_LOCK()`` lock on commit/rollback
    by itself, so the safe sequence is always
    ``GET_LOCK -> mutate -> commit/rollback -> RELEASE_LOCK`` -- never
    releasing before the commit/rollback has actually completed. This
    helper's ``finally`` only ever runs *after* the caller's body (mutation
    and its commit/rollback) has finished, so that ordering is structural,
    not a convention callers have to remember.

    ``GET_LOCK()`` returning ``0`` (timeout) or ``NULL`` (error) raises
    :class:`GatewayRouteBindingLockError` *before* the caller's body ever
    runs: no ``GatewayRouteBinding`` row is touched when the lock cannot be
    acquired.
    """
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    lock_connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        name = gateway_route_binding_lock_name(lock_connection)
        _get_lock(lock_connection, name, timeout_seconds)
        previously_held = session.info.get(SESSION_INFO_LOCK_HELD_KEY, False)
        session.info[SESSION_INFO_LOCK_HELD_KEY] = True
        try:
            yield
        finally:
            session.info[SESSION_INFO_LOCK_HELD_KEY] = previously_held
            _release_lock(lock_connection, name)
    finally:
        lock_connection.close()


def session_holds_gateway_route_binding_lock(session: Session) -> bool:
    """Whether ``session`` is currently inside a
    :func:`gateway_route_binding_write` block -- see
    ``SESSION_INFO_LOCK_HELD_KEY`` for why this exists."""
    return bool(session.info.get(SESSION_INFO_LOCK_HELD_KEY, False))
