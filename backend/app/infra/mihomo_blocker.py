"""Durable, identifier-only blockers for uncertain Mihomo writer state."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Job, JobStatus

MIHOMO_RECONCILE_BLOCKER_JOB_TYPE = "MIHOMO_RECONCILE_BLOCKER"
MIHOMO_BLOCKER_KIND_FINALIZATION = "FINALIZATION"
MIHOMO_BLOCKER_KIND_LOCK_RELEASE = "LOCK_RELEASE"
MIHOMO_LOCK_RELEASE_PENDING = "MIHOMO_LOCK_RELEASE_PENDING"
MIHOMO_LOCK_RELEASE_UNKNOWN = "MIHOMO_LOCK_RELEASE_OUTCOME_UNKNOWN"
_BLOCKER_DEDUPE_PREFIX = "MIHOMO_BLOCKER:"
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,142}\Z")
_SAFE_REASON = re.compile(r"MIHOMO_[A-Z0-9_]{1,78}\Z")


class MihomoBlockerError(RuntimeError):
    """A durable blocker could not be created or matched exactly."""


def _safe_optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise MihomoBlockerError("MIHOMO_BLOCKER_IDENTIFIER_INVALID")
    return value


def _safe_reason(value: object) -> str:
    if not isinstance(value, str) or _SAFE_REASON.fullmatch(value) is None:
        raise MihomoBlockerError("MIHOMO_BLOCKER_REASON_INVALID")
    return value


def ensure_mihomo_blocker(
    db: Session,
    *,
    blocker_kind: str,
    source_job_id: int | None,
    operation_id: str | None,
    snapshot_revision: int | None,
    reason_code: str,
) -> Job:
    """Create or reuse one identifier-only unresolved blocker."""
    kind = _safe_optional_identifier(blocker_kind)
    if kind is None:
        raise MihomoBlockerError("MIHOMO_BLOCKER_KIND_INVALID")
    reason = _safe_reason(reason_code)
    operation = _safe_optional_identifier(operation_id)
    if source_job_id is not None and (
        isinstance(source_job_id, bool) or not isinstance(source_job_id, int) or source_job_id <= 0
    ):
        raise MihomoBlockerError("MIHOMO_BLOCKER_SOURCE_INVALID")
    if snapshot_revision is not None and (
        isinstance(snapshot_revision, bool)
        or not isinstance(snapshot_revision, int)
        or snapshot_revision <= 0
    ):
        raise MihomoBlockerError("MIHOMO_BLOCKER_REVISION_INVALID")
    source_identity = str(source_job_id) if source_job_id is not None else "none"
    dedupe_key = f"{_BLOCKER_DEDUPE_PREFIX}{kind}:{source_identity}"
    if len(dedupe_key) > 160:
        raise MihomoBlockerError("MIHOMO_BLOCKER_DEDUPE_KEY_INVALID")
    payload = {
        "blocker_kind": kind,
        "source_job_id": source_job_id,
        "operation_id": operation,
        "snapshot_revision": snapshot_revision,
        "reason_code": reason,
    }
    existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key).with_for_update())
    if existing is not None:
        if existing.job_type != MIHOMO_RECONCILE_BLOCKER_JOB_TYPE:
            raise MihomoBlockerError("MIHOMO_BLOCKER_IDENTITY_MISMATCH")
        try:
            existing_payload = json.loads(existing.payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MihomoBlockerError("MIHOMO_BLOCKER_IDENTITY_MISMATCH") from exc
        if existing_payload != payload:
            raise MihomoBlockerError("MIHOMO_BLOCKER_IDENTITY_MISMATCH")
        if existing.status is JobStatus.SUCCEEDED:
            existing.status = JobStatus.PENDING
            existing.locked_at = None
            existing.last_error_code = None
        return existing
    blocker = Job(
        job_type=MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
        dedupe_key=dedupe_key,
        payload_json=json.dumps(payload, sort_keys=True),
        status=JobStatus.PENDING,
        attempts=0,
        max_attempts=1,
        available_at=datetime.now(UTC),
    )
    db.add(blocker)
    db.flush()
    return blocker


def resolve_mihomo_blocker(blocker: Job) -> None:
    """Mark a release blocker resolved; the caller owns the DB commit."""
    if blocker.job_type != MIHOMO_RECONCILE_BLOCKER_JOB_TYPE:
        raise MihomoBlockerError("MIHOMO_BLOCKER_IDENTITY_MISMATCH")
    blocker.status = JobStatus.SUCCEEDED
    blocker.locked_at = None
    blocker.last_error_code = None
