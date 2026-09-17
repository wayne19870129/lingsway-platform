"""Durable commit-first gateway reconciliation (ADR-022 Direction B).

This module is the one application/infra entry point for the real gateway
projection.  It reuses the existing generic Job table; payloads contain only
stable identifiers and never tokens, credentials, candidates, or secrets.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import full_desired_routing_snapshot
from backend.app.models import (
    AuditLog,
    GatewayRouteBinding,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.base import GatewayProvider, NotifyEvent
from backend.app.providers.registry import ProviderRegistry

GATEWAY_RECONCILE_JOB_TYPE = "GATEWAY_RECONCILE"
_GATEWAY_RETRY_LIMIT = 5
_STALE_AFTER = timedelta(minutes=15)


class GatewayReconciliationBlocked(RuntimeError):
    """An older durable projection mutation must be resolved first."""


class GatewayReconciliationError(RuntimeError):
    """A secret-safe gateway reconciliation failure."""


class FirstCommitOutcome(StrEnum):
    """Fresh-DB classification after an acknowledgement-loss commit error."""

    LANDED = "LANDED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class FinalizationCommitOutcome(StrEnum):
    """Fresh-DB classification after the second durable commit attempt."""

    LANDED = "LANDED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class GatewayCommitOutcomeUnknown(GatewayReconciliationError):
    """The first authoritative commit cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class GatewayReconciliationResult:
    job_id: int
    applied: bool
    finalized: bool
    retryable: bool
    reason_code: str | None = None


def _safe_reason(error: BaseException) -> str:
    if isinstance(error, GatewayReconciliationError):
        return str(error)
    return f"GATEWAY_RECONCILIATION_{type(error).__name__.upper()}"


def _payload(
    *,
    operation_kind: str,
    subscription_id: int,
    order_id: int | None,
    provision_run_id: str | None,
) -> dict[str, object]:
    if operation_kind not in {"PURCHASE", "RELEASE", "PRINCIPAL_UPDATE"}:
        raise ValueError("unsupported gateway reconciliation operation")
    value: dict[str, object] = {
        "operation_kind": operation_kind,
        "subscription_id": subscription_id,
    }
    if order_id is not None:
        value["order_id"] = order_id
    if provision_run_id is not None:
        value["provision_run_id"] = provision_run_id
    return value


def _job_payload(job: Job) -> dict[str, object]:
    try:
        value = json.loads(job.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_PAYLOAD_INVALID") from exc
    if not isinstance(value, dict):
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_PAYLOAD_INVALID")
    return value


def assert_no_unresolved_gateway_mutation(db: Session) -> None:
    """Block new Class-A writers using a MySQL current read under the named lock."""
    statement = (
        select(Job.id)
        .where(
            Job.job_type == GATEWAY_RECONCILE_JOB_TYPE,
            Job.status != JobStatus.SUCCEEDED,
        )
        .order_by(Job.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing_id = db.scalar(statement)
    if existing_id is not None:
        raise GatewayReconciliationBlocked(
            "an older gateway reconciliation is unresolved; recovery or manual "
            "resolution is required before a new projection mutation"
        )


def _independent_engine(db: Session) -> Engine:
    """Return an Engine so outcome probes never reuse an active Connection."""
    bind = db.get_bind()
    if isinstance(bind, Connection):
        return bind.engine
    if isinstance(bind, Engine):
        return bind
    raise GatewayCommitOutcomeUnknown("GATEWAY_COMMIT_OBSERVER_UNAVAILABLE")


def classify_first_commit_outcome(
    db: Session, *, dedupe_key: str, subscription_id: int
) -> FirstCommitOutcome:
    """Classify an uncertain first commit from a fresh, independent Session.

    The caller keeps the projection named lock while this probe runs. Both
    the intent row and the active route must be present to call the commit
    landed; both absent is the only safe not-landed result. Any partial
    evidence, or an unavailable database, is UNKNOWN and therefore
    fail-closed without external compensation.
    """
    try:
        with Session(
            bind=_independent_engine(db), autoflush=False, expire_on_commit=False
        ) as probe:
            job_id = probe.scalar(
                select(Job.id)
                .where(Job.dedupe_key == dedupe_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            route_id = probe.scalar(
                select(GatewayRouteBinding.id)
                .where(
                    GatewayRouteBinding.subscription_id == subscription_id,
                    GatewayRouteBinding.enabled.is_(True),
                    GatewayRouteBinding.released_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
    except Exception as exc:
        raise GatewayCommitOutcomeUnknown(
            "GATEWAY_FIRST_COMMIT_OUTCOME_UNKNOWN"
        ) from exc
    if job_id is not None and route_id is not None:
        return FirstCommitOutcome.LANDED
    if job_id is None and route_id is None:
        return FirstCommitOutcome.ABSENT
    return FirstCommitOutcome.UNKNOWN


def classify_finalization_outcome(
    db: Session,
    *,
    job_id: int,
    subscription_id: int,
    order_id: int | None,
    provision_run_id: int | None,
    operation_kind: str,
) -> FinalizationCommitOutcome:
    """Classify the second durable commit from an independent fresh observer."""
    try:
        with Session(
            bind=_independent_engine(db), autoflush=False, expire_on_commit=False
        ) as probe:
            job_status = probe.scalar(
                select(Job.status)
                .where(Job.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation_kind == "PURCHASE":
                subscription_status = probe.scalar(
                    select(Subscription.status)
                    .where(Subscription.id == subscription_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if order_id is None:
                    return FinalizationCommitOutcome.UNKNOWN
                order_status = probe.scalar(
                    select(Order.status)
                    .where(Order.id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                run_status = (
                    probe.scalar(
                        select(Job.status)
                        .where(Job.id == provision_run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if provision_run_id is not None
                    else JobStatus.SUCCEEDED
                )
                if (
                    job_status is None
                    or subscription_status is None
                    or order_status is None
                    or run_status is None
                ):
                    return FinalizationCommitOutcome.UNKNOWN
                if (
                    job_status is JobStatus.SUCCEEDED
                    and subscription_status is SubscriptionStatus.ACTIVE
                    and order_status is OrderStatus.ACTIVATED
                    and run_status is JobStatus.SUCCEEDED
                ):
                    return FinalizationCommitOutcome.LANDED
                if (
                    job_status is not JobStatus.SUCCEEDED
                    and subscription_status is not SubscriptionStatus.ACTIVE
                    and order_status is not OrderStatus.ACTIVATED
                    and run_status is not JobStatus.SUCCEEDED
                ):
                    return FinalizationCommitOutcome.ABSENT
                return FinalizationCommitOutcome.UNKNOWN
            if job_status is None:
                return FinalizationCommitOutcome.UNKNOWN
            return (
                FinalizationCommitOutcome.LANDED
                if job_status is JobStatus.SUCCEEDED
                else FinalizationCommitOutcome.ABSENT
            )
    except Exception as exc:
        raise GatewayCommitOutcomeUnknown(
            "GATEWAY_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
        ) from exc


def enqueue_gateway_reconciliation(
    db: Session,
    *,
    operation_kind: str,
    subscription_id: int,
    order_id: int | None = None,
    provision_run_id: str | None = None,
    operation_id: str,
) -> Job:
    """Create the pending record inside the caller's first authoritative commit."""
    payload = _payload(
        operation_kind=operation_kind,
        subscription_id=subscription_id,
        order_id=order_id,
        provision_run_id=provision_run_id,
    )
    dedupe_key = f"GATEWAY_RECONCILE:{operation_kind}:{operation_id}"
    existing = db.scalar(
        select(Job)
        .where(Job.dedupe_key == dedupe_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        return existing
    job = Job(
        job_type=GATEWAY_RECONCILE_JOB_TYPE,
        dedupe_key=dedupe_key,
        payload_json=json.dumps(payload, sort_keys=True),
        status=JobStatus.PENDING,
        attempts=0,
        available_at=datetime.now(UTC),
        max_attempts=_GATEWAY_RETRY_LIMIT,
    )
    db.add(job)
    db.flush()
    return job


def _claim_job(db: Session, now: datetime) -> Job | None:
    stale_before = now - _STALE_AFTER
    job = db.scalar(
        select(Job)
        .where(
            Job.job_type == GATEWAY_RECONCILE_JOB_TYPE,
            or_(
                Job.status.in_([JobStatus.PENDING, JobStatus.FAILED]),
                (Job.status == JobStatus.RUNNING) & (Job.locked_at < stale_before),
            ),
            Job.available_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.attempts += 1
    db.commit()
    return job


def _record_failure(
    db: Session, job_id: int, error: BaseException, now: datetime
) -> GatewayReconciliationResult:
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_JOB_MISSING") from error
    payload = _job_payload(job)
    subscription_id = payload.get("subscription_id")
    subscription = (
        db.get(Subscription, int(subscription_id))
        if isinstance(subscription_id, int)
        else None
    )
    exhausted = job.attempts >= job.max_attempts
    reason_code = _safe_reason(error)
    job.status = JobStatus.FAILED
    job.locked_at = None
    job.last_error_code = reason_code[:80]
    job.available_at = now + timedelta(minutes=min(2 ** max(job.attempts, 1), 60))
    provision_run_id = payload.get("provision_run_id")
    if exhausted and isinstance(provision_run_id, str) and provision_run_id.isdigit():
        provision_run = db.get(Job, int(provision_run_id))
        if provision_run is not None:
            provision_run.status = JobStatus.FAILED
            provision_run.locked_at = None
            provision_run.last_error_code = reason_code[:80]

    if subscription is not None:
        subscription.provision_error = (
            "gateway reconciliation requires manual intervention"
            if exhausted
            else "gateway reconciliation pending retry"
        )
        db.add(
            AuditLog(
                action="GATEWAY_RECONCILIATION_FAILED",
                entity_type="SUBSCRIPTION",
                entity_id=str(subscription.id),
                result="MANUAL" if exhausted else "RETRY",
                detail=(
                    "DB desired state remains authoritative; "
                    + ("manual intervention required" if exhausted else "retry scheduled")
                    + f"; reason={reason_code}"
                ),
            )
        )
    db.commit()
    return GatewayReconciliationResult(
        job_id=job.id,
        applied=False,
        finalized=False,
        retryable=not exhausted,
        reason_code=reason_code,
    )


def _finalize(
    db: Session,
    job: Job,
    payload: dict[str, object],
) -> None:
    subscription_id = payload.get("subscription_id")
    if not isinstance(subscription_id, int):
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_SUBSCRIPTION_INVALID")
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_SUBSCRIPTION_MISSING")
    operation_kind = payload.get("operation_kind")
    if operation_kind == "PURCHASE":
        order_id = payload.get("order_id")
        if not isinstance(order_id, int):
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_ORDER_INVALID")
        order = db.get(Order, order_id)
        if order is None:
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_ORDER_MISSING")
        subscription.status = SubscriptionStatus.ACTIVE
        order.status = OrderStatus.ACTIVATED
        order.activated_at = order.activated_at or datetime.now(UTC)
        subscription.provision_error = None
    job.status = JobStatus.SUCCEEDED
    job.locked_at = None
    job.last_error_code = None
    db.add(
        AuditLog(
            action="GATEWAY_RECONCILIATION_SUCCEEDED",
            entity_type="SUBSCRIPTION",
            entity_id=str(subscription.id),
            result="SUCCESS",
            detail="DB desired state, runtime projection, and finalization committed",
        )
    )
    provision_run_id = payload.get("provision_run_id")
    if isinstance(provision_run_id, str) and provision_run_id.isdigit():
        run = db.get(Job, int(provision_run_id))
        if run is not None:
            run.status = JobStatus.SUCCEEDED
            run.last_error_code = None


def _mark_finalization_unknown(
    db: Session,
    job_id: int,
    payload: dict[str, object],
    now: datetime,
) -> GatewayReconciliationResult:
    """Keep an unresolved, writer-blocking state without guessing commit truth."""
    diagnostic = "GATEWAY_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
    db.rollback()
    try:
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_JOB_MISSING")
        job.status = JobStatus.RUNNING
        job.locked_at = now
        job.last_error_code = diagnostic
        subscription_id = payload.get("subscription_id")
        if isinstance(subscription_id, int):
            subscription = db.scalar(
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if subscription is not None:
                subscription.provision_error = (
                    "gateway finalization requires manual review"
                )
                db.add(
                    AuditLog(
                        action="GATEWAY_FINALIZATION_OUTCOME_UNKNOWN",
                        entity_type="SUBSCRIPTION",
                        entity_id=str(subscription.id),
                        result="MANUAL",
                        detail=(
                            "runtime projection may be applied; finalization "
                            "commit outcome requires review"
                        ),
                    )
                )
        db.commit()
    except Exception as exc:
        with suppress(Exception):
            db.rollback()
        raise GatewayCommitOutcomeUnknown(diagnostic) from exc
    return GatewayReconciliationResult(
        job_id=job_id,
        applied=True,
        finalized=False,
        retryable=False,
        reason_code=diagnostic,
    )


def _reconcile_claimed_job(
    db: Session,
    job: Job,
    registry: ProviderRegistry | GatewayProvider,
) -> GatewayReconciliationResult:
    payload = _job_payload(job)
    gateway = registry.gateway if isinstance(registry, ProviderRegistry) else registry
    job_id = job.id
    try:
        desired = full_desired_routing_snapshot(db)
        resolver = SqlAlchemyCredentialResolver(db)
        candidate = gateway.render(desired, resolver)
        validation = gateway.validate(candidate)
        if not validation.valid:
            raise GatewayReconciliationError("GATEWAY_CANDIDATE_INVALID")
        applied = gateway.apply(candidate)
        if not applied.applied:
            raise GatewayReconciliationError("GATEWAY_APPLY_NOT_CONFIRMED")
        _finalize(db, job, payload)
        subscription_value = payload.get("subscription_id")
        order_value = payload.get("order_id")
        run_value = payload.get("provision_run_id")
        if not isinstance(subscription_value, int):
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_SUBSCRIPTION_INVALID")
        if order_value is not None and not isinstance(order_value, int):
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_ORDER_INVALID")
        if run_value is not None and (
            not isinstance(run_value, str) or not run_value.isdigit()
        ):
            raise GatewayReconciliationError("GATEWAY_RECONCILIATION_RUN_INVALID")
        try:
            db.commit()
        except Exception as commit_error:
            # Release any local locks before the independent certainty probe;
            # a failed COMMIT leaves this Session unable to be the observer.
            with suppress(Exception):
                db.rollback()
            operation_kind = payload.get("operation_kind")
            outcome = classify_finalization_outcome(
                db,
                job_id=job_id,
                subscription_id=subscription_value,
                order_id=order_value,
                provision_run_id=int(run_value) if isinstance(run_value, str) else None,
                operation_kind=(
                    operation_kind if isinstance(operation_kind, str) else ""
                ),
            )
            if outcome is FinalizationCommitOutcome.LANDED:
                with suppress(Exception):
                    db.rollback()
                return GatewayReconciliationResult(job_id, True, True, False)
            if outcome is FinalizationCommitOutcome.ABSENT:
                return _record_failure(db, job_id, commit_error, datetime.now(UTC))
            return _mark_finalization_unknown(db, job_id, payload, datetime.now(UTC))
    except GatewayCommitOutcomeUnknown:
        raise
    except Exception as exc:
        return _record_failure(db, job_id, exc, datetime.now(UTC))
    return GatewayReconciliationResult(job_id, True, True, False)


def _notify_after_lock(
    db: Session, registry: ProviderRegistry, result: GatewayReconciliationResult
) -> None:
    job = db.get(Job, result.job_id)
    if job is None:
        return
    payload = _job_payload(job)
    try:
        registry.notify.send(
            NotifyEvent(
                "GATEWAY_RECONCILIATION_SUCCEEDED",
                {
                    "job_id": result.job_id,
                    "subscription_id": payload.get("subscription_id"),
                    "operation_kind": payload.get("operation_kind"),
                },
            )
        )
    except Exception:
        db.add(
            AuditLog(
                action="GATEWAY_RECONCILIATION_NOTIFY_FAILED",
                entity_type="SUBSCRIPTION",
                entity_id=str(payload.get("subscription_id")),
                result="WARNING",
                detail="reconciliation committed; notification failed",
            )
        )
        db.commit()

def reconcile_gateway_job(
    registry: ProviderRegistry,
    *,
    db_factory: Callable[[], Session] = SessionLocal,
) -> GatewayReconciliationResult | None:
    """Claim and reconcile one job with fresh DB state and the shared named lock."""
    with db_factory() as db:
        with gateway_route_binding_write(db):
            job = _claim_job(db, datetime.now(UTC))
            if job is None:
                return None
            result = _reconcile_claimed_job(db, job, registry)
        if result.applied and result.finalized:
            _notify_after_lock(db, registry, result)
        return result

def reconcile_gateway_job_in_session(
    db: Session,
    job_id: int,
    registry: ProviderRegistry | GatewayProvider,
) -> GatewayReconciliationResult:
    """Run the post-first-commit attempt while the caller still holds the lock."""
    job = db.get(Job, job_id)
    if job is None:
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_JOB_MISSING")
    now = datetime.now(UTC)
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.attempts += 1
    db.commit()
    return _reconcile_claimed_job(db, job, registry)
