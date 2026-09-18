from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.infra.mihomo_reconciliation import (
    CommitOutcome,
    MihomoReconciliationResult,
    ProjectionCandidate,
    ProjectionVerification,
    classify_finalization_outcome,
    enqueue_mihomo_reconciliation,
    reconcile_mihomo_job,
)
from backend.app.models import Job, JobStatus
from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import ControllerSecretResolver


@pytest.fixture
def mysql_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Mihomo reconciliation tests")
    engine = build_engine(database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1))
    if engine.dialect.name != "mysql":
        engine.dispose()
        pytest.skip("SKIP LOCKED/current-read coverage is MySQL-only")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_mysql_enqueue_commit_and_classifier(mysql_engine: Engine) -> None:
    with Session(mysql_engine, expire_on_commit=False) as db:
        job = enqueue_mihomo_reconciliation(
            db, operation_id="mysql-r1", operation_kind="RECONCILE", snapshot_revision=1
        )
        db.commit()
        assert job.status is JobStatus.PENDING
        payload = json.loads(job.payload_json)
        payload["finalization_evidence"] = {
            "operation_id": "mysql-r1",
            "operation_kind": "RECONCILE",
            "snapshot_revision": 1,
            "candidate_fingerprint": "projection-v1",
            "verification": "readback-v1",
        }
        job.payload_json = json.dumps(payload)
        job.status = JobStatus.SUCCEEDED
        db.commit()
        assert classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="mysql-r1",
            operation_kind="RECONCILE",
            snapshot_revision=1,
            candidate_fingerprint="projection-v1",
        ) is CommitOutcome.LANDED


class _Candidate:
    version = "projection-v1"


class _Provider:
    def __init__(self) -> None:
        self.apply_count = 0
        self._count_lock = Lock()

    def render(self, desired: DesiredForwarderState) -> object:
        return desired

    def finalize(self, template: object, resolver: object) -> ProjectionCandidate:
        return _Candidate()

    def apply(self, candidate: ProjectionCandidate) -> object:
        with self._count_lock:
            self.apply_count += 1
        return SimpleNamespace(applied=True)


class _Loader:
    def load(self, db: Session, job: Job) -> DesiredForwarderState:
        return DesiredForwarderState(snapshot_revision=1, snapshot_identity="mysql-v1")


class _Verifier:
    def verify(
        self, candidate: object, *, operation_id: str, snapshot_revision: int
    ) -> ProjectionVerification:
        return ProjectionVerification(True, "readback-v1")


def _run_worker(engine: Engine, provider: _Provider) -> MihomoReconciliationResult | None:
    return reconcile_mihomo_job(
        cast(object, provider),
        _Loader(),
        cast(ControllerSecretResolver, SimpleNamespace(resolve=lambda ref: None)),
        _Verifier(),
        db_factory=lambda: Session(engine, expire_on_commit=False),
    )


def test_mysql_two_workers_only_one_applies(mysql_engine: Engine) -> None:
    with Session(mysql_engine, expire_on_commit=False) as db:
        job = enqueue_mihomo_reconciliation(
            db, operation_id="mysql-workers", operation_kind="RECONCILE", snapshot_revision=1
        )
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        job_id = job.id

    provider = _Provider()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _run_worker(mysql_engine, provider), range(2)))

    assert provider.apply_count == 1
    assert sum(result is not None and result.finalized for result in results) == 1
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("status", "attempts", "locked_at", "last_error_code"),
    [
        (JobStatus.RUNNING, 1, datetime.now(UTC), None),
        (JobStatus.FAILED, 5, None, "MIHOMO_ROLLBACK_VERIFIED"),
        (
            JobStatus.RUNNING,
            1,
            datetime.now(UTC) - timedelta(minutes=20),
            "MIHOMO_ROLLBACK_OUTCOME_UNKNOWN",
        ),
    ],
)
def test_mysql_older_unresolved_intent_blocks_newer_writer(
    mysql_engine: Engine,
    status: JobStatus,
    attempts: int,
    locked_at: datetime | None,
    last_error_code: str | None,
) -> None:
    with Session(mysql_engine, expire_on_commit=False) as db:
        older = enqueue_mihomo_reconciliation(
            db, operation_id="mysql-older", operation_kind="RECONCILE", snapshot_revision=1
        )
        older.status = status
        older.attempts = attempts
        older.locked_at = locked_at
        older.last_error_code = last_error_code
        older.available_at = datetime.now(UTC) - timedelta(seconds=1)
        newer = enqueue_mihomo_reconciliation(
            db, operation_id="mysql-newer", operation_kind="RECONCILE", snapshot_revision=1
        )
        db.commit()
        newer_id = newer.id

    provider = _Provider()
    result = _run_worker(mysql_engine, provider)

    assert result is None
    assert provider.apply_count == 0
    with Session(mysql_engine) as db:
        newer = db.get(Job, newer_id)
        assert newer is not None
        assert newer.status is JobStatus.PENDING
