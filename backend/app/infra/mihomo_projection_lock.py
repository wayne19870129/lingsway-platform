from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

DEFAULT_LOCK_TIMEOUT_SECONDS = 30
_LOCK_NAME_MAX_LENGTH = 64
SESSION_INFO_LOCK_HELD_KEY = "mihomo_projection_lock_held"


class MihomoProjectionLockError(RuntimeError):
    """The Mihomo global writer lock could not be safely acquired/released."""


def mihomo_projection_lock_name(bind: Connection | Engine) -> str:
    engine: Any = bind.engine if isinstance(bind, Connection) else bind
    database = engine.url.database
    if not database:
        raise MihomoProjectionLockError("cannot derive Mihomo projection lock name")
    name = f"lingsway:{database}:mihomo-full-config"
    if len(name) > _LOCK_NAME_MAX_LENGTH:
        raise MihomoProjectionLockError("Mihomo projection lock name exceeds MySQL limit")
    return name


def _get_lock(connection: Connection, name: str, timeout_seconds: int) -> None:
    try:
        result = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": name, "timeout": timeout_seconds},
        ).scalar()
    except Exception as exc:
        connection.invalidate()
        raise MihomoProjectionLockError(
            "failed to acquire Mihomo projection lock; connection state unknown"
        ) from exc
    if result != 1:
        raise MihomoProjectionLockError(
            f"failed to acquire Mihomo projection lock: GET_LOCK returned {result!r}"
        )


def _release_lock(connection: Connection, name: str) -> None:
    try:
        result = connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name}).scalar()
    except Exception:
        connection.invalidate()
        raise
    if result != 1:
        connection.invalidate()
        raise MihomoProjectionLockError("Mihomo projection lock release was not confirmed")


@contextmanager
def mihomo_projection_write(
    session: Session, *, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    raw: Connection | None = None
    try:
        bind = session.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        raw = engine.connect()
        connection = raw.execution_options(isolation_level="AUTOCOMMIT")
        name = mihomo_projection_lock_name(connection)
    except MihomoProjectionLockError:
        raise
    except Exception as exc:
        with suppress(Exception):
            if raw is not None:
                raw.close()
        raise MihomoProjectionLockError("failed to open Mihomo projection lock connection") from exc
    try:
        _get_lock(connection, name, timeout_seconds)
        previous = bool(session.info.get(SESSION_INFO_LOCK_HELD_KEY, False))
        session.info[SESSION_INFO_LOCK_HELD_KEY] = True
        try:
            yield
        finally:
            session.info[SESSION_INFO_LOCK_HELD_KEY] = previous
            _release_lock(connection, name)
    finally:
        connection.close()


def session_holds_mihomo_projection_lock(session: Session) -> bool:
    return bool(session.info.get(SESSION_INFO_LOCK_HELD_KEY, False))
