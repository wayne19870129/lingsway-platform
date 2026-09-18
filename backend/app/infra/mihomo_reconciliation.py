from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.infra.mihomo_projection_lock import (
    mihomo_projection_write,
    session_holds_mihomo_projection_lock,
)
from backend.app.models import Job, JobStatus
from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import ControllerSecretResolver

MIHOMO_RECONCILE_JOB_TYPE = "MIHOMO_RECONCILE"
MIHOMO_OPERATION_KINDS = frozenset(
    {"FULL_PROJECTION", "RECONCILE", "CREATE", "UPDATE", "DELETE"}
)
MIHOMO_FINALIZATION_UNKNOWN = "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
_RETRY_LIMIT = 5
_STALE_AFTER = timedelta(minutes=15)


class MihomoReconciliationError(RuntimeError):
    pass


class MihomoReconciliationBlocked(MihomoReconciliationError):
    pass


class MihomoCommitOutcomeUnknown(MihomoReconciliationError):
    pass


class CommitOutcome(StrEnum):
    LANDED = "LANDED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProjectionVerification:
    verified: bool
    evidence_version: str


class MihomoDesiredSnapshotLoader(Protocol):
    def load(self, db: Session, job: Job) -> DesiredForwarderState: ...


class ProjectionVerifier(Protocol):
    def verify(
        self, candidate: object, *, operation_id: str, snapshot_revision: int
    ) -> ProjectionVerification: ...


class ProjectionCandidate(Protocol):
    @property
    def version(self) -> str: ...


class MihomoProjectionProvider(Protocol):
    def render(self, desired: DesiredForwarderState) -> object: ...

    def finalize(
        self, template: object, resolver: ControllerSecretResolver
    ) -> ProjectionCandidate: ...

    def apply(self, candidate: ProjectionCandidate) -> object: ...


@dataclass(frozen=True, slots=True)
class MihomoReconciliationResult:
    job_id: int
    applied: bool
    finalized: bool
    retryable: bool
    reason_code: str | None = None


def _validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("operation_id must be a non-empty canonical string")
    return value


def _validate_operation_kind(value: object) -> str:
    if not isinstance(value, str) or value not in MIHOMO_OPERATION_KINDS:
        raise ValueError("operation_kind is not supported")
    return value


def _validate_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("snapshot_revision must be a positive integer")
    return value


def _payload(job: Job) -> dict[str, object]:
    try:
        value = json.loads(job.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MihomoReconciliationError("MIHOMO_RECONCILIATION_PAYLOAD_INVALID") from exc
    if not isinstance(value, dict):
        raise MihomoReconciliationError("MIHOMO_RECONCILIATION_PAYLOAD_INVALID")
    return value


def enqueue_mihomo_reconciliation(
    db: Session, *, operation_id: str, operation_kind: str, snapshot_revision: int
) -> Job:
    operation_id = _validate_operation_id(operation_id)
    operation_kind = _validate_operation_kind(operation_kind)
    revision = _validate_revision(snapshot_revision)
    key = f"MIHOMO_RECONCILE:{operation_id}"
    existing = db.scalar(select(Job).where(Job.dedupe_key == key).with_for_update())
    if existing is not None:
        try:
            payload = _payload(existing)
            identity_matches = (
                existing.job_type == MIHOMO_RECONCILE_JOB_TYPE
                and payload.get("operation_id") == operation_id
                and payload.get("operation_kind") == operation_kind
                and payload.get("snapshot_revision") == revision
            )
        except MihomoReconciliationError:
            identity_matches = False
        if not identity_matches:
            raise MihomoReconciliationError("MIHOMO_RECONCILIATION_IDENTITY_MISMATCH")
        return existing
    job = Job(
        job_type=MIHOMO_RECONCILE_JOB_TYPE,
        dedupe_key=key,
        payload_json=json.dumps(
            {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "snapshot_revision": revision,
            },
            sort_keys=True,
        ),
        status=JobStatus.PENDING,
        attempts=0,
        max_attempts=_RETRY_LIMIT,
        available_at=datetime.now(UTC),
    )
    db.add(job)
    db.flush()
    return job


def assert_no_unresolved_mihomo_mutation(
    db: Session, *, owning_job_id: int | None = None
) -> None:
    if not session_holds_mihomo_projection_lock(db):
        raise MihomoReconciliationBlocked("MIHOMO_PROJECTION_LOCK_REQUIRED")
    filters = [
        Job.job_type == MIHOMO_RECONCILE_JOB_TYPE,
        Job.status != JobStatus.SUCCEEDED,
    ]
    if owning_job_id is not None:
        filters.append(Job.id < owning_job_id)
    row = db.scalar(
        select(Job).where(*filters).order_by(Job.id).limit(1).with_for_update()
    )
    if row is not None:
        raise MihomoReconciliationBlocked("MIHOMO_UNRESOLVED_INTENT_BLOCKS_WRITER")


def _engine(db: Session) -> Engine:
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    if not isinstance(engine, Engine):
        raise MihomoReconciliationError("MIHOMO_COMMIT_OBSERVER_UNAVAILABLE")
    return engine


def classify_finalization_outcome(
    db: Session,
    *,
    job_id: int,
    operation_id: str,
    snapshot_revision: int,
    candidate_fingerprint: str,
    operation_kind: str | None = None,
) -> CommitOutcome:
    """Classify using a fresh independent observer; ambiguity is never ABSENT."""
    try:
        with Session(bind=_engine(db), autoflush=False, expire_on_commit=False) as observer:
            job = observer.get(Job, job_id)
            if job is None or job.job_type != MIHOMO_RECONCILE_JOB_TYPE:
                return CommitOutcome.UNKNOWN
            payload = _payload(job)
            if (
                payload.get("operation_id") != operation_id
                or payload.get("snapshot_revision") != snapshot_revision
                or (
                    operation_kind is not None
                    and payload.get("operation_kind") != operation_kind
                )
            ):
                return CommitOutcome.UNKNOWN
            evidence = payload.get("finalization_evidence")
            if job.status is JobStatus.SUCCEEDED:
                if not isinstance(evidence, dict):
                    return CommitOutcome.UNKNOWN
                if (
                    evidence.get("operation_id") == operation_id
                    and evidence.get("operation_kind") == payload.get("operation_kind")
                    and evidence.get("snapshot_revision") == snapshot_revision
                    and evidence.get("candidate_fingerprint") == candidate_fingerprint
                    and isinstance(evidence.get("verification"), str)
                    and bool(evidence["verification"])
                ):
                    return CommitOutcome.LANDED
                return CommitOutcome.UNKNOWN
            if evidence is None and job.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.FAILED,
            }:
                return CommitOutcome.ABSENT
            return CommitOutcome.UNKNOWN
    except Exception:
        return CommitOutcome.UNKNOWN


def _claim(db: Session, now: datetime) -> Job | None:
    stale = now - _STALE_AFTER
    job = db.scalar(
        select(Job)
        .where(
            Job.job_type == MIHOMO_RECONCILE_JOB_TYPE,
            or_(
                Job.status.in_([JobStatus.PENDING, JobStatus.FAILED]),
                (Job.status == JobStatus.RUNNING)
                & (Job.locked_at < stale)
                & or_(
                    Job.last_error_code.is_(None),
                    Job.last_error_code != MIHOMO_FINALIZATION_UNKNOWN,
                ),
            ),
            Job.available_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status, job.locked_at = JobStatus.RUNNING, now
    job.attempts += 1
    db.commit()
    return job


def _failure(
    db: Session, job_id: int, error: BaseException, now: datetime
) -> MihomoReconciliationResult:
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        raise MihomoReconciliationError("MIHOMO_RECONCILIATION_JOB_MISSING") from error
    exhausted = job.attempts >= job.max_attempts
    code = (
        str(error)
        if isinstance(error, MihomoReconciliationError)
        else "MIHOMO_RECONCILIATION_RUNTIME_FAILURE"
    )
    job.status, job.locked_at, job.last_error_code = JobStatus.FAILED, None, code[:80]
    job.available_at = now + timedelta(minutes=min(2 ** max(job.attempts, 1), 60))
    db.commit()
    return MihomoReconciliationResult(job_id, False, False, not exhausted, code[:80])


def _mark_unknown(
    db: Session, job_id: int, *, applied: bool = True
) -> MihomoReconciliationResult:
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        raise MihomoReconciliationError("MIHOMO_RECONCILIATION_JOB_MISSING")
    job.status = JobStatus.RUNNING
    job.locked_at = datetime.now(UTC)
    job.last_error_code = MIHOMO_FINALIZATION_UNKNOWN
    try:
        db.commit()
    except Exception as exc:
        raise MihomoCommitOutcomeUnknown(MIHOMO_FINALIZATION_UNKNOWN) from exc
    return MihomoReconciliationResult(
        job_id, applied, False, False, MIHOMO_FINALIZATION_UNKNOWN
    )


def _reset_blocked_claim(db: Session, job: Job) -> None:
    db.rollback()
    current = db.get(Job, job.id)
    if current is not None and current.status is JobStatus.RUNNING:
        current.status = JobStatus.PENDING
        current.locked_at = None
        current.attempts = max(current.attempts - 1, 0)
        db.commit()


def reconcile_mihomo_job(
    provider: MihomoProjectionProvider,
    loader: MihomoDesiredSnapshotLoader,
    resolver: ControllerSecretResolver,
    verifier: ProjectionVerifier,
    *,
    db_factory: Callable[[], Session] = SessionLocal,
) -> MihomoReconciliationResult | None:
    with db_factory() as db, mihomo_projection_write(db):
        job = _claim(db, datetime.now(UTC))
        if job is None:
            return None
        try:
            assert_no_unresolved_mihomo_mutation(db, owning_job_id=job.id)
        except MihomoReconciliationBlocked:
            _reset_blocked_claim(db, job)
            return None
        try:
            payload = _payload(job)
            operation_id = _validate_operation_id(payload.get("operation_id"))
            operation_kind = _validate_operation_kind(payload.get("operation_kind"))
            expected = _validate_revision(payload.get("snapshot_revision"))
            desired = loader.load(db, job)
            if desired.snapshot_revision != expected:
                raise MihomoReconciliationError("MIHOMO_STALE_DESIRED_SNAPSHOT")
            template = provider.render(desired)
            candidate = provider.finalize(template, resolver)
            fingerprint = candidate.version
            apply_result = provider.apply(candidate)
            if getattr(apply_result, "applied", False) is not True:
                raise MihomoReconciliationError("MIHOMO_APPLY_NOT_CONFIRMED")
            try:
                verification = verifier.verify(
                    candidate, operation_id=operation_id, snapshot_revision=expected
                )
            except Exception:
                return _mark_unknown(db, job.id)
            if (
                not isinstance(verification, ProjectionVerification)
                or not verification.verified
                or not isinstance(verification.evidence_version, str)
                or not verification.evidence_version
            ):
                return _mark_unknown(db, job.id)
            payload["finalization_evidence"] = {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "snapshot_revision": expected,
                "candidate_fingerprint": fingerprint,
                "verification": verification.evidence_version,
            }
            job.payload_json = json.dumps(payload, sort_keys=True)
            job.status, job.locked_at, job.last_error_code = JobStatus.SUCCEEDED, None, None
            try:
                db.commit()
            except Exception as commit_error:
                db.rollback()
                outcome = classify_finalization_outcome(
                    db,
                    job_id=job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                )
                if outcome is CommitOutcome.LANDED:
                    return MihomoReconciliationResult(job.id, True, True, False)
                if outcome is CommitOutcome.ABSENT:
                    return _failure(db, job.id, commit_error, datetime.now(UTC))
                return _mark_unknown(db, job.id)
            return MihomoReconciliationResult(job.id, True, True, False)
        except MihomoCommitOutcomeUnknown:
            raise
        except Exception as exc:
            return _failure(db, job.id, exc, datetime.now(UTC))
