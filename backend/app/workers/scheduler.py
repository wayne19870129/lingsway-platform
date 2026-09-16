"""Registry-backed scheduler for accounting, transport, and usage jobs."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.models import (
    AuditLog,
    ProviderStatus,
    Subscription,
    SubscriptionStatus,
    TransportProviderKind,
    TransportProviderRecord,
    UsagePeriod,
)
from backend.app.providers.base import AccountingProvider
from backend.app.providers.registry import ProviderRegistry, build_registry
from backend.app.workers.accounting_sync import (
    reconcile_usage_period_cache,
    run_next_usage_job,
    run_pending_reconcile,
)
from backend.app.workers.drift_check import check_egress_drift
from backend.app.infra.gateway_reconciliation import reconcile_gateway_job
from backend.app.workers.transport_sync import refresh_provider_inventory

NORMAL_USAGE_INTERVAL_SECONDS = 300
FAST_USAGE_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class SchedulerBatchResult:
    transport_records: int
    reconciled_subscriptions: int
    usage_jobs: int
    fast_usage_poll: bool
    egress_endpoints_checked: int
    egress_drift_findings: int

    @property
    def processed_jobs(self) -> int:
        return self.reconciled_subscriptions + self.usage_jobs


def build_scheduler_registry() -> ProviderRegistry:
    """Build the complete provider set from environment configuration."""
    return build_registry(get_settings())


def sync_transport_capacity_and_inventory(db: Any, registry: ProviderRegistry) -> int:
    records = db.scalars(
        select(TransportProviderRecord).where(
            TransportProviderRecord.enabled.is_(True),
            TransportProviderRecord.kind == TransportProviderKind.SUBSCRIPTION,
        )
    ).all()
    completed = 0
    for record in records:
        try:
            refresh_provider_inventory(db, record, registry.transport)
        except Exception:
            db.rollback()
            failed = db.get(TransportProviderRecord, record.id)
            transitioned = failed.status != ProviderStatus.DEGRADED
            failed.status = ProviderStatus.DEGRADED
            if transitioned:
                db.add(
                    AuditLog(
                        action="TRANSPORT_PROVIDER_SYNC_FAILED",
                        entity_type="TRANSPORT_PROVIDER",
                        entity_id=str(failed.id),
                        result="FAILURE",
                        detail="Previous provider cache retained; inspect provider connectivity.",
                    )
                )
            db.commit()
            continue
        completed += 1
    return completed


def usage_requires_fast_poll(db: Any, provider: AccountingProvider) -> bool:
    subscriptions = db.scalars(
        select(Subscription).where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
            Subscription.accounting_user_id.is_not(None),
        )
    ).all()
    for subscription in subscriptions:
        if subscription.current_period_id is None:
            continue
        period = db.get(UsagePeriod, subscription.current_period_id)
        if period is None or period.quota_bytes <= 0:
            continue
        usage = provider.get_usage(subscription.accounting_user_id)
        if usage.used_bytes * 100 >= period.quota_bytes * 90:
            return True
    return False


def usage_poll_interval_seconds(fast_usage_poll: bool) -> int:
    return FAST_USAGE_INTERVAL_SECONDS if fast_usage_poll else NORMAL_USAGE_INTERVAL_SECONDS


def run_batch(
    registry: ProviderRegistry,
    db_factory: Callable[[], Any] = SessionLocal,
) -> SchedulerBatchResult:
    """Run one scheduler tick using only providers assembled by the registry.

    TASK-T16 Phase 2B8 (independent-review round 4, Major 2): takes an
    already-built ``registry`` instead of a ``registry_factory`` default
    that self-constructed one via ``build_scheduler_registry()``. That
    default was itself an unmanaged-construction path -- calling this
    function directly (its own default argument, not just a test double)
    would build a registry nothing ever closes. ``build_scheduler_registry()``
    now only ever runs at the actual process ownership boundary, ``main()``.
    """
    with db_factory() as db:
        reconcile_gateway_job(registry, db_factory=db_factory)
        transport_records = sync_transport_capacity_and_inventory(db, registry)
        reconciled = run_pending_reconcile(
            db, registry.accounting, get_settings().accounting_sync_batch_size
        )
        reconcile_usage_period_cache(db)
        usage_jobs = 0
        for _ in range(get_settings().accounting_sync_batch_size):
            job = run_next_usage_job(db, registry.accounting, gateway=registry.gateway)
            if job is None:
                break
            usage_jobs += 1
        fast_usage_poll = usage_requires_fast_poll(db, registry.accounting)
        drift_result = check_egress_drift(db, registry.egress)
    return SchedulerBatchResult(
        transport_records=transport_records,
        reconciled_subscriptions=reconciled,
        usage_jobs=usage_jobs,
        fast_usage_poll=fast_usage_poll,
        egress_endpoints_checked=drift_result.checked,
        egress_drift_findings=len(drift_result.findings),
    )


def main() -> None:
    """TASK-T16 Phase 2B8: build exactly one process-lifetime
    ``ProviderRegistry`` and reuse it across every batch this process
    runs -- looping or one-shot alike -- closing it deterministically in
    ``finally`` on both normal and exceptional exit. Previously each
    ``run_batch()`` call (every loop iteration, and even the single
    one-shot call) used its own default ``registry_factory`` argument
    (``build_scheduler_registry``), so a real, resource-owning provider
    would have leaked a fresh, never-closed ``httpx.Client`` every 60/300
    seconds.
    """
    parser = argparse.ArgumentParser(description="Run registry-backed scheduler jobs")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=NORMAL_USAGE_INTERVAL_SECONDS)
    args = parser.parse_args()
    registry = build_scheduler_registry()
    try:
        while True:
            result = run_batch(registry)
            if not args.loop:
                return
            normal_interval = max(args.interval, NORMAL_USAGE_INTERVAL_SECONDS)
            interval = FAST_USAGE_INTERVAL_SECONDS if result.fast_usage_poll else normal_interval
            time.sleep(interval)
    finally:
        registry.close()


if __name__ == "__main__":
    main()
