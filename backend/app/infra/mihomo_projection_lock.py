from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.infra.mihomo_blocker import (
    MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
    MIHOMO_LOCK_RELEASE_PENDING,
    MIHOMO_LOCK_RELEASE_UNKNOWN,
    ensure_mihomo_blocker,
    resolve_mihomo_blocker,
)

DEFAULT_LOCK_TIMEOUT_SECONDS = 30
_LOCK_NAME_MAX_LENGTH = 64
SESSION_INFO_LOCK_HELD_KEY = "mihomo_projection_lock_held"
SESSION_INFO_RELEASE_METADATA_KEY = "mihomo_projection_release_metadata"


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
    connection: Connection | None = None
    acquired = False
    try:
        bind = session.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        raw = engine.connect()
        connection = raw.execution_options(isolation_level="AUTOCOMMIT")
        name = mihomo_projection_lock_name(connection)
        _get_lock(connection, name, timeout_seconds)
        acquired = True
    except MihomoProjectionLockError:
        with suppress(Exception):
            if raw is not None and not acquired:
                raw.close()
        raise
    except Exception as exc:
        with suppress(Exception):
            if raw is not None and not acquired:
                raw.close()
        raise MihomoProjectionLockError("failed to open Mihomo projection lock connection") from exc
    assert connection is not None
    release_blocker = None
    try:
        previous = bool(session.info.get(SESSION_INFO_LOCK_HELD_KEY, False))
        session.info[SESSION_INFO_LOCK_HELD_KEY] = True
        try:
            yield
        finally:
            session.info[SESSION_INFO_LOCK_HELD_KEY] = previous
            metadata = session.info.get(SESSION_INFO_RELEASE_METADATA_KEY)
            if isinstance(metadata, dict):
                try:
                    session.rollback()
                    release_blocker = ensure_mihomo_blocker(
                        session,
                        blocker_kind=MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
                        source_job_id=metadata.get("source_job_id"),
                        operation_id=metadata.get("operation_id"),
                        snapshot_revision=metadata.get("snapshot_revision"),
                        reason_code=MIHOMO_LOCK_RELEASE_PENDING,
                    )
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    connection.invalidate()
                    raise MihomoProjectionLockError(
                        "Mihomo release blocker persistence was not confirmed"
                    ) from exc
            try:
                _release_lock(connection, name)
            except Exception as exc:
                if release_blocker is not None:
                    try:
                        session.rollback()
                        release_blocker.last_error_code = MIHOMO_LOCK_RELEASE_UNKNOWN
                        session.commit()
                    except Exception:
                        session.rollback()
                raise MihomoProjectionLockError(
                    "Mihomo projection lock release outcome is unknown"
                ) from exc
            if release_blocker is not None:
                try:
                    session.rollback()
                    resolve_mihomo_blocker(release_blocker)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    raise MihomoProjectionLockError(
                        "Mihomo release blocker cleanup was not confirmed"
                    ) from exc
    finally:
        session.info.pop(SESSION_INFO_RELEASE_METADATA_KEY, None)
        connection.close()


def session_holds_mihomo_projection_lock(session: Session) -> bool:
    return bool(session.info.get(SESSION_INFO_LOCK_HELD_KEY, False))
