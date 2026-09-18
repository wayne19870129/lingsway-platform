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
    MIHOMO_FINALIZATION_UNKNOWN,
    MIHOMO_ROLLBACK_UNKNOWN,
    MIHOMO_RUNTIME_UNKNOWN,
    CommitOutcome,
    MihomoReconciliationBlocked,
    MihomoReconciliationError,
    MihomoReconciliationResult,
    ProjectionCandidate,
    ProjectionVerification,
    ProjectionVerifier,
    _failure,
    _mark_unknown,
    assert_no_unresolved_mihomo_mutation,
    classify_finalization_outcome,
    enqueue_mihomo_reconciliation,
    reconcile_mihomo_job,
)
from backend.app.models import Job, JobStatus
from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretResolver,
    MihomoForwarderProvider,
    MihomoRollbackUnknownError,
    MihomoRollbackVerifiedError,
    MihomoRuntime,
)
from backend.app.providers.forwarder.mihomo_projection import (
    TransportMaterialization,
    TransportMaterializationReference,
)


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
    ) is CommitOutcome.UNKNOWN
    job.status = JobStatus.RUNNING
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


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.FAILED])
def test_classifier_never_calls_pending_or_failed_absent(
    db: Session, status: JobStatus
) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-certainty", operation_kind="RECONCILE", snapshot_revision=1
    )
    job.status = status
    db.commit()

    assert classify_finalization_outcome(
        db,
        job_id=job.id,
        operation_id="op-certainty",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
    ) is CommitOutcome.UNKNOWN


def test_partial_finalization_evidence_is_preserved_as_unknown(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-partial", operation_kind="RECONCILE", snapshot_revision=1
    )
    payload = json.loads(job.payload_json)
    payload["finalization_evidence"] = {
        "operation_id": "op-partial",
        "candidate_fingerprint": "projection-v1",
    }
    job.status = JobStatus.RUNNING
    job.payload_json = json.dumps(payload, sort_keys=True)
    db.commit()

    result = _mark_unknown(
        db,
        job.id,
        operation_id="op-partial",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
        diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
    )

    assert result.finalized is False
    assert result.retryable is False
    current = db.get(Job, job.id)
    assert current is not None
    assert current.status is JobStatus.RUNNING
    assert json.loads(current.payload_json)["finalization_evidence"] == payload[
        "finalization_evidence"
    ]
    assert current.last_error_code is None


def test_ack_loss_after_durable_commit_is_landed_without_downgrade(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-ack-loss", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def commit_then_lose_ack() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")
        original_commit()

    monkeypatch.setattr(db, "commit", commit_then_lose_ack)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result == MihomoReconciliationResult(1, True, True, False)
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.last_error_code is None


def test_commit_before_durable_write_is_absent_and_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-before-commit", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def fail_final_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("commit failed before durable write")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_final_commit)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.finalized is False
    assert result.retryable is True
    assert result.reason_code == "MIHOMO_RECONCILIATION_RUNTIME_FAILURE"
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.FAILED


def test_observer_unavailable_after_ack_loss_preserves_exact_landed_row(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-observer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def commit_then_observer_fails() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_commit()
            raise RuntimeError("acknowledgement lost")
        original_commit()

    def unavailable(_db: Session) -> object:
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(db, "commit", commit_then_observer_fails)
    monkeypatch.setattr(reconciliation, "_engine", unavailable)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result == MihomoReconciliationResult(1, True, True, False)
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.last_error_code is None


def test_observer_unavailable_with_exact_running_row_is_manual_not_failed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-observer-running", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def fail_final_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("commit acknowledgement unavailable")
        original_commit()

    def unavailable(_db: Session) -> object:
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(db, "commit", fail_final_commit)
    monkeypatch.setattr(reconciliation, "_engine", unavailable)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.finalized is False
    assert result.retryable is False
    assert result.reason_code == MIHOMO_FINALIZATION_UNKNOWN
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == MIHOMO_FINALIZATION_UNKNOWN


@pytest.mark.parametrize(
    "evidence_version",
    ["", " readback-v1", "readback-v1 ", "readback\nv1", "x" * 65, None, 1],
)
def test_evidence_version_is_canonical_and_secret_neutral(
    db: Session,
    evidence_version: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-evidence", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    verification = ProjectionVerification(True, cast(str, evidence_version))
    result = _reconcile_with_fake_lock(db, provider, _Verifier(verification), monkeypatch)

    assert result is not None
    assert result.finalized is False
    assert result.retryable is False
    assert result.reason_code == MIHOMO_RUNTIME_UNKNOWN
    job = db.get(Job, 1)
    assert job is not None
    assert "never-store" not in job.payload_json
    assert "finalization_evidence" not in job.payload_json


def test_rollback_verified_failure_is_bounded_retryable(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-retry", operation_kind="RECONCILE", snapshot_revision=1
    )
    job.status = JobStatus.RUNNING
    job.attempts = job.max_attempts
    db.commit()

    result = _failure(db, job.id, MihomoRollbackVerifiedError(), datetime.now(UTC))

    assert result.retryable is False
    assert result.reason_code == "MIHOMO_ROLLBACK_VERIFIED"
    current = db.get(Job, job.id)
    assert current is not None
    assert current.status is JobStatus.FAILED
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="op-retry-newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


def test_stale_desired_snapshot_never_renders_or_applies(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaleLoader:
        def load(self, db: Session, job: Job) -> DesiredForwarderState:
            return DesiredForwarderState(snapshot_revision=2, snapshot_identity="db-v2")

    enqueue_mihomo_reconciliation(
        db, operation_id="op-stale", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    db.info["mihomo_projection_lock_held"] = True
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        StaleLoader(),
        cast(ControllerSecretResolver, SimpleNamespace(resolve=lambda ref: None)),
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_STALE_DESIRED_SNAPSHOT"
    assert provider.render_count == 0
    assert provider.apply_count == 0


def test_stale_transport_materialization_fails_before_runtime_mutation(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaleMaterializationLoader:
        def load(self, db: Session, job: Job) -> DesiredForwarderState:
            stale = TransportMaterialization(
                owner_record_id=1,
                provider_code="subscription",
                source_revision=1,
                cache_identity="cache-v1",
                content_hash="not-used-after-stale-check",
                freshness_deadline=datetime.now(UTC) - timedelta(seconds=1),
                content={"proxies": []},
            )
            reference = TransportMaterializationReference(
                owner_record_id=1,
                provider_code="subscription",
                source_revision=1,
                cache_identity="cache-v1",
            )
            return DesiredForwarderState(
                listener_specs=(),
                rules=(),
                transport_materializations=(stale,),
                transport_references=(reference,),
                snapshot_revision=1,
                snapshot_identity="db-v1",
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                },
            )

    enqueue_mihomo_reconciliation(
        db, operation_id="op-stale-material", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = MihomoForwarderProvider(cast(MihomoRuntime, object()))
    db.info["mihomo_projection_lock_held"] = True
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        StaleMaterializationLoader(),
        cast(ControllerSecretResolver, SimpleNamespace(resolve=lambda ref: None)),
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_TRANSPORT_CACHE_STALE"
    assert result.retryable is True
class _Provider:
    def __init__(self) -> None:
        self.render_count = 0
        self.finalize_count = 0
        self.apply_count = 0
        self.apply_error: Exception | None = None

    def render(self, desired: DesiredForwarderState) -> object:
        self.render_count += 1
        return SimpleNamespace(version="projection-v1")

    def finalize(self, template: object, resolver: object) -> ProjectionCandidate:
        self.finalize_count += 1
        return _Candidate()

    def apply(self, candidate: ProjectionCandidate) -> object:
        self.apply_count += 1
        if self.apply_error is not None:
            raise self.apply_error
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


def test_rollback_verified_is_failed_and_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-verified", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_error = MihomoRollbackVerifiedError()

    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_ROLLBACK_VERIFIED"
    assert result.retryable is True
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.FAILED


def test_rollback_unknown_is_manual_nonreclaimable_and_blocks_newer_writer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_error = MihomoRollbackUnknownError()

    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.reason_code == MIHOMO_ROLLBACK_UNKNOWN
    assert result.retryable is False
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == MIHOMO_ROLLBACK_UNKNOWN
    from backend.app.infra.mihomo_reconciliation import _claim

    job.locked_at = datetime.now(UTC) - timedelta(minutes=20)
    job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert _claim(db, datetime.now(UTC)) is None
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


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
    assert result.reason_code == "MIHOMO_RUNTIME_PROJECTION_UNKNOWN"
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == "MIHOMO_RUNTIME_PROJECTION_UNKNOWN"
    assert provider.apply_count == 1
