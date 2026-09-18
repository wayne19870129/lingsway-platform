from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.infra import mihomo_reconciliation as reconciliation
from backend.app.infra.mihomo_reconciliation import (
    CommitOutcome,
    MihomoReconciliationBlocked,
    MihomoReconciliationError,
    MihomoReconciliationResult,
    ProjectionCandidate,
    ProjectionVerification,
    ProjectionVerifier,
    assert_no_unresolved_mihomo_mutation,
    classify_finalization_outcome,
    enqueue_mihomo_reconciliation,
    reconcile_mihomo_job,
)
from backend.app.models import Job, JobStatus
from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import ControllerSecretResolver


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    cast(Table, Job.__table__).create(bind=engine)
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_enqueue_identity_is_transactional_and_exact(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    db.rollback()
    assert db.get(Job, job.id) is None

    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    db.commit()
    same = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    assert same.id == job.id
    with pytest.raises(MihomoReconciliationError, match="IDENTITY_MISMATCH"):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="UPDATE", snapshot_revision=2
        )
    with pytest.raises(MihomoReconciliationError, match="IDENTITY_MISMATCH"):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=3
        )


def test_enqueue_rejects_noncanonical_operation_identity(db: Session) -> None:
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db, operation_id=" op-1", operation_kind="RECONCILE", snapshot_revision=1
        )
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="ARBITRARY", snapshot_revision=1
        )


def test_guard_requires_lock_and_blocks_older_unresolved_intent(db: Session) -> None:
    older = enqueue_mihomo_reconciliation(
        db, operation_id="older", operation_kind="RECONCILE", snapshot_revision=1
    )
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    with pytest.raises(MihomoReconciliationBlocked, match="LOCK_REQUIRED"):
        assert_no_unresolved_mihomo_mutation(db)
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked, match="UNRESOLVED_INTENT"):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)
    older.status = JobStatus.FAILED
    older.attempts = older.max_attempts
    db.commit()
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


def test_claims_pending_retryable_and_stale_running_but_not_unknown(
    db: Session,
) -> None:
    from backend.app.infra.mihomo_reconciliation import _claim

    pending = enqueue_mihomo_reconciliation(
        db, operation_id="claim-pending", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    claimed = _claim(db, now)
    assert claimed is not None and claimed.id == pending.id
    claimed.status = JobStatus.FAILED
    claimed.locked_at = None
    claimed.available_at = now - timedelta(seconds=1)
    db.commit()
    retry = _claim(db, now)
    assert retry is not None and retry.id == pending.id
    retry.status = JobStatus.SUCCEEDED
    retry.locked_at = None
    db.commit()

    stale = enqueue_mihomo_reconciliation(
        db, operation_id="claim-stale", operation_kind="RECONCILE", snapshot_revision=1
    )
    stale.status = JobStatus.RUNNING
    stale.locked_at = now - timedelta(minutes=20)
    stale.available_at = now - timedelta(seconds=1)
    db.commit()
    claimed_stale = _claim(db, now)
    assert claimed_stale is not None and claimed_stale.id == stale.id
    claimed_stale.status = JobStatus.SUCCEEDED
    claimed_stale.locked_at = None
    db.commit()

    unknown = enqueue_mihomo_reconciliation(
        db, operation_id="claim-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    unknown.status = JobStatus.RUNNING
    unknown.locked_at = now - timedelta(minutes=20)
    unknown.last_error_code = "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
    unknown.available_at = now - timedelta(seconds=1)
    db.commit()
    assert _claim(db, now) is None


def test_classifier_requires_exact_job_and_evidence(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-2", operation_kind="RECONCILE", snapshot_revision=4
    )
    db.commit()
    assert classify_finalization_outcome(
        db,
        job_id=job.id,
        operation_id="op-2",
        operation_kind="RECONCILE",
        snapshot_revision=4,
        candidate_fingerprint="v4",
    ) is CommitOutcome.ABSENT
    assert classify_finalization_outcome(
        db,
        job_id=9999,
        operation_id="op-2",
        operation_kind="RECONCILE",
        snapshot_revision=4,
        candidate_fingerprint="v4",
    ) is CommitOutcome.UNKNOWN
    payload = json.loads(job.payload_json)
    payload["finalization_evidence"] = {
        "operation_id": "op-2",
        "operation_kind": "RECONCILE",
        "snapshot_revision": 4,
        "candidate_fingerprint": "v4",
        "verification": "readback-v1",
    }
    job.payload_json = json.dumps(payload)
    job.status = JobStatus.SUCCEEDED
    db.commit()
    assert classify_finalization_outcome(
        db,
        job_id=job.id,
        operation_id="op-2",
        operation_kind="RECONCILE",
        snapshot_revision=4,
        candidate_fingerprint="v4",
    ) is CommitOutcome.LANDED
    payload["finalization_evidence"]["candidate_fingerprint"] = "wrong"
    job.payload_json = json.dumps(payload)
    db.commit()
    assert classify_finalization_outcome(
        db,
        job_id=job.id,
        operation_id="op-2",
        operation_kind="RECONCILE",
        snapshot_revision=4,
        candidate_fingerprint="v4",
    ) is CommitOutcome.UNKNOWN


class _Provider:
    def __init__(self) -> None:
        self.apply_count = 0

    def render(self, desired: DesiredForwarderState) -> object:
        return SimpleNamespace(version="projection-v1")

    def finalize(self, template: object, resolver: object) -> ProjectionCandidate:
        return _Candidate()

    def apply(self, candidate: ProjectionCandidate) -> object:
        self.apply_count += 1
        return SimpleNamespace(applied=True)


class _Candidate:
    version = "projection-v1"
    content = {"secret": "never-store"}


class _Loader:
    def load(self, db: Session, job: Job) -> DesiredForwarderState:
        return DesiredForwarderState(snapshot_revision=1, snapshot_identity="db-v1")


class _Verifier:
    def __init__(self, result: object) -> None:
        self.result = result

    def verify(
        self, candidate: object, *, operation_id: str, snapshot_revision: int
    ) -> ProjectionVerification | object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _reconcile_with_fake_lock(
    db: Session,
    provider: _Provider,
    verifier: _Verifier,
    monkeypatch: pytest.MonkeyPatch,
) -> MihomoReconciliationResult | None:
    db.info["mihomo_projection_lock_held"] = True
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    return reconcile_mihomo_job(
        provider,
        _Loader(),
        cast(ControllerSecretResolver, SimpleNamespace(resolve=lambda ref: None)),
        cast(ProjectionVerifier, verifier),
        db_factory=lambda: db,
    )


def test_positive_verification_finalizes_with_secret_neutral_version(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-3", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )
    assert result is not None
    assert result.finalized is True
    job = db.get(Job, 1)
    assert job is not None
    assert "never-store" not in job.payload_json
    assert "projection-v1" in job.payload_json


@pytest.mark.parametrize(
    "verification",
    [ProjectionVerification(False, "readback-v1"), None, ValueError("observer unavailable")],
)
def test_post_apply_verification_uncertainty_is_manual_and_blocking(
    db: Session, verification: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-4", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    result = _reconcile_with_fake_lock(db, provider, _Verifier(verification), monkeypatch)
    assert result is not None
    assert result.retryable is False
    assert result.finalized is False
    assert result.reason_code == "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
    assert provider.apply_count == 1
