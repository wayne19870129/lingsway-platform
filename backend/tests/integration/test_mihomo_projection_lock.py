from __future__ import annotations

import os
from collections.abc import Iterator
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import build_engine
from backend.app.infra import mihomo_projection_lock as lock_module
from backend.app.infra.mihomo_blocker import MIHOMO_RECONCILE_BLOCKER_JOB_TYPE
from backend.app.infra.mihomo_projection_lock import (
    SESSION_INFO_RELEASE_METADATA_KEY,
    MihomoProjectionLockError,
    _get_lock,
    _release_lock,
    mihomo_projection_lock_name,
    mihomo_projection_write,
    session_holds_mihomo_projection_lock,
)
from backend.app.infra.mihomo_reconciliation import (
    MihomoReconciliationBlocked,
    assert_no_unresolved_mihomo_mutation,
)
from backend.app.models import Job, JobStatus


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


def _release_metadata() -> dict[str, object]:
    return {
        "source_job_id": 991,
        "operation_id": "release-certainty",
        "snapshot_revision": 1,
    }


def test_mysql_release_resolves_pending_blocker_after_positive_release(
    mysql_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_release = lock_module._release_lock
    observed_pending = False

    def observe_then_release(connection: object, name: str) -> None:
        nonlocal observed_pending
        with Session(mysql_engine) as observer:
            blocker = observer.scalar(
                select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
            )
            observed_pending = blocker is not None and blocker.status is JobStatus.PENDING
        original_release(connection, name)  # type: ignore[arg-type]

    monkeypatch.setattr(lock_module, "_release_lock", observe_then_release)
    with Session(mysql_engine) as db:
        db.info[SESSION_INFO_RELEASE_METADATA_KEY] = _release_metadata()
        with mihomo_projection_write(db):
            assert session_holds_mihomo_projection_lock(db)

    with Session(mysql_engine) as db:
        blocker = db.scalar(
            select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
        )
        assert observed_pending is True
        assert blocker is not None
        assert blocker.status is JobStatus.SUCCEEDED

    with Session(mysql_engine) as db, mihomo_projection_write(db):
        db.info["mihomo_projection_lock_held"] = True
        assert_no_unresolved_mihomo_mutation(db)


@pytest.mark.parametrize("release_failure", ["exception", "zero", "null"])
def test_mysql_release_uncertainty_keeps_global_blocker(
    mysql_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    release_failure: str,
) -> None:
    def uncertain_release(connection: object, name: str) -> None:
        raise MihomoProjectionLockError(f"RELEASE_LOCK returned {release_failure}")

    monkeypatch.setattr(lock_module, "_release_lock", uncertain_release)
    with Session(mysql_engine) as db:
        db.info[SESSION_INFO_RELEASE_METADATA_KEY] = _release_metadata()
        with pytest.raises(
            MihomoProjectionLockError, match="release outcome is unknown"
        ), mihomo_projection_write(db):
            pass

    with Session(mysql_engine) as db:
        blocker = db.scalar(
            select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
        )
        assert blocker is not None
        assert blocker.status is not JobStatus.SUCCEEDED
        db.info["mihomo_projection_lock_held"] = True
        with pytest.raises(MihomoReconciliationBlocked, match="GLOBAL_MANUAL_BLOCKER"):
            assert_no_unresolved_mihomo_mutation(db)


def test_mysql_release_success_but_blocker_clear_commit_failure_keeps_blocker(
    mysql_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(mysql_engine, expire_on_commit=False) as db:
        original_commit = db.commit
        commit_calls = 0

        def fail_cleanup_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise RuntimeError("blocker cleanup commit lost")
            original_commit()

        monkeypatch.setattr(db, "commit", fail_cleanup_commit)
        db.info[SESSION_INFO_RELEASE_METADATA_KEY] = _release_metadata()
        with pytest.raises(
            MihomoProjectionLockError, match="cleanup was not confirmed"
        ), mihomo_projection_write(db):
            pass

    with Session(mysql_engine) as db:
        blocker = db.scalar(
            select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
        )
        assert blocker is not None
        assert blocker.status is not JobStatus.SUCCEEDED


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
