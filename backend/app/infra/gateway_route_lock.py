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

#: How long GET_LOCK() blocks waiting for the lock before giving up. Chosen
#: to comfortably exceed one provisioning saga's external-call budget
#: (egress/accounting/gateway/notify calls all happen while a provisioning
#: writer holds this lock) while still bounding how long a queued writer
#: is blocked before it gets a clear, actionable fail-closed error instead
#: of hanging indefinitely.
DEFAULT_LOCK_TIMEOUT_SECONDS = 30

#: MySQL's documented GET_LOCK() name length limit (in bytes/characters).
_LOCK_NAME_MAX_LENGTH = 64


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
    connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


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
        try:
            yield
        finally:
            _release_lock(lock_connection, name)
    finally:
        lock_connection.close()
