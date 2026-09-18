from __future__ import annotations

import os
from collections.abc import Iterator
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.core.database import build_engine
from backend.app.infra.mihomo_projection_lock import (
    MihomoProjectionLockError,
    _get_lock,
    _release_lock,
    mihomo_projection_lock_name,
    mihomo_projection_write,
    session_holds_mihomo_projection_lock,
)


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


class _Connection:
    def __init__(self, value: object = 1, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.invalidated = False

    def execute(self, statement: object, params: object) -> _Result:
        if self.error is not None:
            raise self.error
        return _Result(self.value)

    def invalidate(self) -> None:
        self.invalidated = True


@pytest.fixture
def mysql_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for MySQL named-lock tests")
    engine = build_engine(database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1))
    if engine.dialect.name != "mysql":
        engine.dispose()
        pytest.skip("GET_LOCK coverage is MySQL-only")
    try:
        yield engine
    finally:
        engine.dispose()


def test_database_scoped_name_and_commit_span(mysql_engine: Engine) -> None:
    with Session(mysql_engine) as db:
        assert mihomo_projection_lock_name(mysql_engine).endswith(
            ":mihomo-full-config"
        )
        with mihomo_projection_write(db, timeout_seconds=1):
            assert session_holds_mihomo_projection_lock(db)
            db.commit()
            assert session_holds_mihomo_projection_lock(db)
        assert not session_holds_mihomo_projection_lock(db)


def test_mysql_named_lock_blocks_second_contender_until_release(mysql_engine: Engine) -> None:
    owner_ready = Event()
    release_owner = Event()
    contender_entered = Event()
    errors: list[BaseException] = []

    def owner() -> None:
        try:
            with Session(mysql_engine) as db, mihomo_projection_write(
                db, timeout_seconds=3
            ):
                owner_ready.set()
                release_owner.wait(5)
        except BaseException as exc:
            errors.append(exc)
            owner_ready.set()

    def contender() -> None:
        try:
            with Session(mysql_engine) as db, mihomo_projection_write(
                db, timeout_seconds=3
            ):
                contender_entered.set()
        except BaseException as exc:
            errors.append(exc)

    owner_thread = Thread(target=owner)
    contender_thread = Thread(target=contender)
    owner_thread.start()
    assert owner_ready.wait(5)
    contender_thread.start()
    assert not contender_entered.wait(0.25)
    release_owner.set()
    owner_thread.join(5)
    contender_thread.join(5)

    assert contender_entered.is_set()
    assert not errors


def test_lock_name_fails_closed_without_database() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    with pytest.raises(MihomoProjectionLockError):
        mihomo_projection_lock_name(engine)


@pytest.mark.parametrize("value", [0, None])
def test_get_lock_timeout_or_null_fails_closed(value: object) -> None:
    with pytest.raises(MihomoProjectionLockError):
        _get_lock(_Connection(value), "lingsway:test:mihomo-full-config", 1)  # type: ignore[arg-type]


def test_get_lock_exception_invalidates_connection() -> None:
    connection = _Connection(error=RuntimeError("db failure"))
    with pytest.raises(MihomoProjectionLockError):
        _get_lock(connection, "lingsway:test:mihomo-full-config", 1)  # type: ignore[arg-type]
    assert connection.invalidated


def test_release_failure_invalidates_connection() -> None:
    connection = _Connection(value=0)
    with pytest.raises(MihomoProjectionLockError):
        _release_lock(connection, "lingsway:test:mihomo-full-config")  # type: ignore[arg-type]
    assert connection.invalidated


def test_lock_name_includes_database_and_respects_limit() -> None:
    bind = SimpleNamespace(url=SimpleNamespace(database="billing"))
    assert mihomo_projection_lock_name(bind).endswith(":billing:mihomo-full-config")  # type: ignore[arg-type]
    long_name = SimpleNamespace(url=SimpleNamespace(database="x" * 60))
    with pytest.raises(MihomoProjectionLockError):
        mihomo_projection_lock_name(long_name)  # type: ignore[arg-type]
