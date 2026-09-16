"""Durable commit-first gateway reconciliation (ADR-022 Direction B).

This module is the one application/infra entry point for the real gateway
projection.  It reuses the existing generic Job table; payloads contain only
stable identifiers and never tokens, credentials, candidates, or secrets.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import full_desired_routing_snapshot
from backend.app.models import (
    AuditLog,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    ReconcileState,
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


def assert_no_unresolved_gateway_mutation(db: Session, subscription_id: int | None = None) -> None:
    """Block every new Class-A writer behind an older unresolved intent."""
    statement = select(Job).where(
        Job.job_type == GATEWAY_RECONCILE_JOB_TYPE,
        Job.status != JobStatus.SUCCEEDED,
    )
    if subscription_id is not None:
        statement = statement.where(
            Job.payload_json.like(f'%"subscription_id": {subscription_id}%')
        )
    existing = db.scalar(statement.order_by(Job.id).limit(1))
    if existing is not None:
        raise GatewayReconciliationBlocked(
            "an older gateway reconciliation is unresolved; recovery or manual "
            "resolution is required before a new projection mutation"
        )


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
    existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
    if existing is not None:
        return existing
    job = Job(
        job_type=GATEWAY_RECONCILE_JOB_TYPE,
        dedupe_key=dedupe_key,
        payload_json=json.dumps(payload, sort_keys=True),
        status=JobStatus.PENDING,
        attempts=0,
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


def _record_failure(db: Session, job_id: int, error: BaseException, now: datetime) -> GatewayReconciliationResult:
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        raise GatewayReconciliationError("GATEWAY_RECONCILIATION_JOB_MISSING") from error
    payload = _job_payload(job)
    subscription_id = payload.get("subscription_id")
    subscription = db.get(Subscription, int(subscription_id)) if isinstance(subscription_id, int) else None
    exhausted = job.attempts >= job.max_attempts
    reason_code = _safe_reason(error)
    job.status = JobStatus.FAILED
    job.locked_at = None
    job.last_error_code = reason_code[:80]
    job.available_at = now + timedelta(minutes=min(2 ** max(job.attempts, 1), 60))
    if subscription is not None:
        subscription.reconcile_state = ReconcileState.FAILED if exhausted else ReconcileState.PENDING
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
    subscription.reconcile_state = ReconcileState.SYNCED
    subscription.reconcile_attempts = 0
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


def _reconcile_claimed_job(
    db: Session,
    job: Job,
    registry: ProviderRegistry | GatewayProvider,
) -> GatewayReconciliationResult:
    payload = _job_payload(job)
    gateway = registry.gateway if isinstance(registry, ProviderRegistry) else registry
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
        db.commit()
    except Exception as exc:
        return _record_failure(db, job.id, exc, datetime.now(UTC))
    return GatewayReconciliationResult(job.id, True, True, False)


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
        if result.applied:
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
