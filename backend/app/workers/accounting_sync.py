"""Database-backed accounting reconciliation and usage jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.infra.gateway_reconciliation import (
    assert_no_unresolved_gateway_mutation,
    enqueue_gateway_reconciliation,
    reconcile_gateway_job_in_session,
)
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.models import (
    AuditLog,
    EgressBinding,
    EgressEndpoint,
    GatewayRouteBinding,
    Job,
    JobStatus,
    ReconcileState,
    Subscription,
    SubscriptionStatus,
    UsageAlert,
    UsageDelta,
    UsageDeltaReason,
    UsagePeriod,
    UsagePeriodStatus,
    UsageSample,
)
from backend.app.providers.base import AccountingProvider, GatewayProvider


def as_utc(value: datetime) -> datetime:
    """MySQL preserves timezone intent; SQLite test storage may return naive values."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def record_usage_alerts(db: Session, subscription: Subscription, period: UsagePeriod) -> None:
    if period.quota_bytes <= 0:
        return
    percent = (period.used_bytes * 100) // period.quota_bytes
    for threshold in (70, 90, 100):
        if percent < threshold:
            continue
        existing = db.scalar(
            select(UsageAlert).where(
                UsageAlert.period_id == period.id,
                UsageAlert.threshold == threshold,
            )
        )
        if existing is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    UsageAlert(
                        subscription_id=subscription.id,
                        period_id=period.id,
                        threshold=threshold,
                    )
                )
                db.flush()
        except IntegrityError:
            recovered = db.scalar(
                select(UsageAlert).where(
                    UsageAlert.period_id == period.id,
                    UsageAlert.threshold == threshold,
                )
            )
            if recovered is None:
                raise


def sync_subscription_usage(
    db: Session,
    subscription: Subscription,
    provider: AccountingProvider,
    now: datetime | None = None,
    gateway: GatewayProvider | None = None,
) -> int:
    if not subscription.accounting_user_id:
        raise ValueError("Subscription has no accounting identity")
    now = now or datetime.now(UTC)
    usage = provider.get_usage(subscription.accounting_user_id)

    locked = db.scalar(
        select(Subscription).where(Subscription.id == subscription.id).with_for_update()
    )
    if locked is None or locked.current_period_id is None:
        raise ValueError("Subscription has no active usage period")
    period = db.scalar(
        select(UsagePeriod)
        .where(UsagePeriod.id == locked.current_period_id)
        .with_for_update()
    )
    if period is None:
        raise ValueError("Current usage period not found")

    previous = db.scalar(
        select(UsageSample)
        .where(
            UsageSample.subscription_id == locked.id,
            UsageSample.node_id == "default",
        )
        .order_by(UsageSample.sampled_at.desc(), UsageSample.id.desc())
        .limit(1)
        .with_for_update()
    )
    current = max(0, usage.used_bytes)
    sample = UsageSample(
        subscription_id=locked.id,
        node_id="default",
        source_counter=current,
        sampled_at=now,
    )
    db.add(sample)
    db.flush()

    if previous is None:
        delta_bytes = current
        reason = UsageDeltaReason.BOOTSTRAP
    elif current >= previous.source_counter:
        delta_bytes = current - previous.source_counter
        reason = UsageDeltaReason.NORMAL
    else:
        delta_bytes = current
        reason = UsageDeltaReason.COUNTER_RESET

    delta = UsageDelta(
        subscription_id=locked.id,
        period_id=period.id,
        node_id="default",
        from_sample_id=previous.id if previous else None,
        to_sample_id=sample.id,
        delta_bytes=delta_bytes,
        reason=reason,
    )
    try:
        with db.begin_nested():
            db.add(delta)
            db.flush()
    except IntegrityError:
        recovered = db.scalar(
            select(UsageDelta).where(
                UsageDelta.subscription_id == locked.id,
                UsageDelta.node_id == "default",
                UsageDelta.to_sample_id == sample.id,
            )
        )
        if recovered is None:
            raise
        return recovered.delta_bytes

    period.used_bytes += delta_bytes
    locked.last_usage_synced_at = now
    record_usage_alerts(db, locked, period)
    needs_reconcile = apply_expiry_policy(db, locked, now)
    rolled_period = locked.current_period_id != period.id
    release_requested = locked.status == SubscriptionStatus.EXPIRED
    db.commit()
    if release_requested:
        release_egress(db, locked.id, now, gateway=gateway)
    if needs_reconcile:
        try:
            reconcile_subscription(
                db, locked, provider, pending_manual_on_failure=rolled_period
            )
        except Exception:
            # The PENDING state was committed before the external write and is
            # intentionally left for the background retry loop.
            db.rollback()
    return delta_bytes


def apply_expiry_policy(db: Session, subscription: Subscription, now: datetime) -> bool:
    """Close an exhausted/expired period and activate the FIFO queued period."""
    period = subscription.current_period
    if (
        period is not None
        and subscription.status == SubscriptionStatus.ACTIVE
        and (period.used_bytes >= period.quota_bytes or now >= as_utc(period.period_end))
    ):
            queued = db.scalars(
                select(UsagePeriod)
                .where(
                    UsagePeriod.subscription_id == subscription.id,
                    UsagePeriod.status == UsagePeriodStatus.QUEUED,
                )
                .order_by(UsagePeriod.id)
                .with_for_update()
            ).all()
            if queued:
                period.status = UsagePeriodStatus.CLOSED
                next_period = queued[0]
                next_period.status = UsagePeriodStatus.ACTIVE
                next_period.period_start = now
                next_period.period_end = now + timedelta(days=30)
                subscription.current_period_id = next_period.id
                if next_period.plan_id is not None:
                    subscription.plan_id = next_period.plan_id
                subscription.service_expire_at = next_period.period_end
                subscription.status = SubscriptionStatus.ACTIVE
                subscription.reconcile_state = ReconcileState.PENDING
                return True
            period.status = UsagePeriodStatus.CLOSED
            subscription.status = SubscriptionStatus.GRACE
            subscription.service_expire_at = now + timedelta(
                hours=get_settings().grace_period_hours
            )
            subscription.reconcile_state = ReconcileState.PENDING
            return True
    service_expire_at = as_utc(subscription.service_expire_at)
    if subscription.status == SubscriptionStatus.ACTIVE and now >= service_expire_at:
        subscription.status = SubscriptionStatus.GRACE
        subscription.service_expire_at = service_expire_at + timedelta(
            hours=get_settings().grace_period_hours
        )
        subscription.reconcile_state = ReconcileState.PENDING
        return True
    if subscription.status == SubscriptionStatus.GRACE and now >= service_expire_at:
        subscription.status = SubscriptionStatus.EXPIRED
    return False


def reconcile_subscription(
    db: Session,
    subscription: Subscription,
    provider: AccountingProvider,
    *,
    pending_manual_on_failure: bool = False,
) -> None:
    """Overwrite the accounting provider from current DB truth."""
    if not subscription.accounting_user_id or subscription.current_period_id is None:
        raise ValueError("Subscription is not ready for reconcile")
    period = db.get(UsagePeriod, subscription.current_period_id)
    if period is None:
        raise ValueError("Current usage period not found")

    subscription.reconcile_state = ReconcileState.PENDING
    subscription.reconcile_attempts += 1
    db.commit()
    try:
        try:
            provider.get_usage(subscription.accounting_user_id)
        except Exception as exc:
            db.rollback()
            current = db.get(Subscription, subscription.id)
            if current is None:
                raise ValueError("Subscription disappeared during reconcile") from exc
            current.reconcile_state = ReconcileState.PENDING
            db.add(
                AuditLog(
                    action="ACCOUNTING_USER_MISSING",
                    entity_type="SUBSCRIPTION",
                    entity_id=str(subscription.id),
                    result="FAILED",
                    detail=f"external accounting identity check failed: {type(exc).__name__}",
                )
            )
            db.commit()
            raise
        provider.set_quota(subscription.accounting_user_id, period.quota_bytes)
        provider.set_expire(subscription.accounting_user_id, as_utc(subscription.service_expire_at))
    except Exception as exc:
        db.rollback()
        current = db.get(Subscription, subscription.id)
        if current is None:
            raise ValueError("Subscription disappeared during reconcile") from exc
        current.reconcile_state = (
            ReconcileState.PENDING_MANUAL
            if pending_manual_on_failure
            else ReconcileState.PENDING
        )
        if pending_manual_on_failure:
            current.provision_error = str(exc)
        db.commit()
        raise
    current = db.get(Subscription, subscription.id)
    if current is None:
        raise ValueError("Subscription disappeared during reconcile")
    current.reconcile_state = ReconcileState.SYNCED
    current.reconcile_attempts = 0
    db.commit()


def run_pending_reconcile(
    db: Session, provider: AccountingProvider, limit: int = 100
) -> int:
    subscriptions = db.scalars(
        select(Subscription)
        .where(Subscription.reconcile_state == ReconcileState.PENDING)
        .order_by(Subscription.id)
        .limit(limit)
    ).all()
    completed = 0
    for subscription in subscriptions:
        try:
            reconcile_subscription(db, subscription, provider)
        except Exception:
            continue
        completed += 1
    return completed


def reconcile_usage_period_cache(db: Session) -> int:
    repaired = 0
    period_ids = db.scalars(select(UsagePeriod.id).order_by(UsagePeriod.id)).all()
    for period_id in period_ids:
        period = db.scalar(
            select(UsagePeriod).where(UsagePeriod.id == period_id).with_for_update()
        )
        if period is None:
            continue
        authoritative = db.scalar(
            select(func.coalesce(func.sum(UsageDelta.delta_bytes), 0)).where(
                UsageDelta.period_id == period.id
            )
        )
        authoritative = int(authoritative or 0)
        if period.used_bytes == authoritative:
            continue
        old_value = period.used_bytes
        period.used_bytes = authoritative
        db.add(
            AuditLog(
                action="USAGE_CACHE_REPAIRED",
                entity_type="USAGE_PERIOD",
                entity_id=str(period.id),
                result="SUCCESS",
                detail=f"cached={old_value},authoritative={authoritative}",
            )
        )
        repaired += 1
    db.commit()
    return repaired


def enqueue_usage_sync(db: Session, subscription_id: int, cycle_key: str) -> Job:
    dedupe_key = f"USAGE_SYNC:{subscription_id}:{cycle_key}"
    existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
    if existing:
        return existing
    job = Job(
        job_type="USAGE_SYNC",
        dedupe_key=dedupe_key,
        payload_json=json.dumps({"subscription_id": subscription_id}),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        recovered = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if recovered is None:
            raise
        return recovered
    db.refresh(job)
    return job


def run_next_usage_job(
    db: Session,
    provider: AccountingProvider,
    now: datetime | None = None,
    gateway: GatewayProvider | None = None,
) -> Job | None:
    now = now or datetime.now(UTC)
    stale_before = now - timedelta(minutes=15)
    job = db.scalar(
        select(Job)
        .where(
            Job.job_type == "USAGE_SYNC",
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
    job_id = job.id
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.attempts += 1
    db.commit()
    try:
        subscription_id = int(json.loads(job.payload_json)["subscription_id"])
        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            raise ValueError("Subscription not found")
        sync_subscription_usage(db, subscription, provider, now, gateway)
        job = db.get(Job, job_id)
        if job is None:
            raise ValueError("Usage job disappeared after sync")
        job.status = JobStatus.SUCCEEDED
        job.last_error_code = None
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is None:
            raise ValueError("Usage job disappeared during failure handling") from exc
        job.status = JobStatus.FAILED
        job.last_error_code = type(exc).__name__
        job.available_at = now + timedelta(minutes=min(2**job.attempts, 60))
    job.locked_at = None
    db.commit()
    db.refresh(job)
    return job


def release_egress(
    db: Session,
    subscription_id: int,
    now: datetime | None = None,
    *,
    gateway: GatewayProvider | None = None,
    operation_id: str | None = None,
) -> bool:
    """Commit an expiry/release desired-state change before runtime mutation.

    The usage transaction is committed by its caller before this function is
    entered. This function owns the release transaction and always records a
    durable gateway intent before asking the provider to remove the route.
    """
    now = now or datetime.now(UTC)
    with gateway_route_binding_write(db):
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        binding = db.scalar(
            select(EgressBinding)
            .where(
                EgressBinding.subscription_id == subscription_id,
                EgressBinding.released_at.is_(None),
            )
            .with_for_update()
        )
        if binding is None:
            return False
        assert_no_unresolved_gateway_mutation(db)
        egress = db.scalar(
            select(EgressEndpoint)
            .where(EgressEndpoint.id == binding.egress_id)
            .with_for_update()
        )
        binding.released_at = now
        gateway_binding = db.scalar(
            select(GatewayRouteBinding)
            .where(
                GatewayRouteBinding.subscription_id == subscription_id,
                GatewayRouteBinding.released_at.is_(None),
            )
            .with_for_update()
        )
        if gateway_binding is not None:
            gateway_binding.enabled = False
            gateway_binding.released_at = now
        if egress is not None:
            egress.current_count = 0
            egress.status = "QUARANTINED"
        job = enqueue_gateway_reconciliation(
            db,
            operation_kind="RELEASE",
            subscription_id=subscription_id,
            order_id=subscription.order_id,
            operation_id=operation_id or f"{subscription_id}:{binding.id}",
        )
        db.commit()
        if gateway is None:
            return False
        result = reconcile_gateway_job_in_session(db, job.id, gateway)
        return result.applied
