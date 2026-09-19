from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.models import (
    Customer,
    Order,
    Plan,
    ReconcileState,
    Subscription,
    SubscriptionStatus,
    UsagePeriod,
    UsagePeriodStatus,
)
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.registry import ProviderRegistry, build_registry
from backend.app.workers import scheduler
from backend.app.workers.accounting_sync import apply_expiry_policy, reconcile_subscription
from backend.app.workers.scheduler import (
    FAST_USAGE_INTERVAL_SECONDS,
    NORMAL_USAGE_INTERVAL_SECONDS,
    SchedulerBatchResult,
    run_batch,
    usage_poll_interval_seconds,
)


class EmptyResult:
    def all(self) -> list[object]:
        return []


class EmptySession:
    def __enter__(self) -> EmptySession:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def scalars(self, statement: object) -> EmptyResult:
        del statement
        return EmptyResult()

    def scalar(self, statement: object) -> None:
        del statement
        return None

    def get(self, model: object, identity: object) -> None:
        del model, identity
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_scheduler_runs_one_mock_round_without_external_services() -> None:
    settings = Settings()
    result = run_batch(
        build_registry(settings),
        db_factory=EmptySession,
    )
    assert result.transport_records == 0
    assert result.reconciled_subscriptions == 0
    assert result.usage_jobs == 0
    assert result.fast_usage_poll is False
    assert result.egress_endpoints_checked == 0
    assert result.egress_drift_findings == 0


def test_usage_poll_interval_switches_between_five_and_one_minutes() -> None:
    assert usage_poll_interval_seconds(False) == NORMAL_USAGE_INTERVAL_SECONDS == 300
    assert usage_poll_interval_seconds(True) == FAST_USAGE_INTERVAL_SECONDS == 60


class _QueuedResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def all(self) -> list[object]:
        return self.values


class _RolloverDb:
    def __init__(self, queued: list[object]) -> None:
        self.queued = queued

    def scalars(self, statement: object) -> _QueuedResult:
        del statement
        return _QueuedResult(self.queued)


def _rollover_state(*, used: int, end: datetime, queued: list[object]) -> tuple[Any, Any, Any]:
    subscription = SimpleNamespace(
        id=1,
        status=SubscriptionStatus.ACTIVE,
        service_expire_at=end,
        current_period_id=1,
        plan_id=10,
        reconcile_state=ReconcileState.SYNCED,
    )
    current = SimpleNamespace(
        id=1,
        used_bytes=used,
        quota_bytes=100,
        period_end=end,
        status=UsagePeriodStatus.ACTIVE,
    )
    subscription.current_period = current
    return subscription, current, _RolloverDb(queued)


def test_quota_exhaustion_rolls_to_fifo_queued_period() -> None:
    now = datetime.now(UTC)
    first = SimpleNamespace(
        id=2,
        plan_id=20,
        status=UsagePeriodStatus.QUEUED,
        period_start=now,
        period_end=now,
        quota_bytes=200,
    )
    second = SimpleNamespace(
        id=3,
        plan_id=30,
        status=UsagePeriodStatus.QUEUED,
        period_start=now,
        period_end=now,
        quota_bytes=300,
    )
    subscription, current, db = _rollover_state(
        used=100, end=now + timedelta(days=1), queued=[first, second]
    )
    assert apply_expiry_policy(db, subscription, now) is True
    assert current.status is UsagePeriodStatus.CLOSED
    assert first.status is UsagePeriodStatus.ACTIVE
    assert second.status is UsagePeriodStatus.QUEUED
    assert subscription.current_period_id == first.id
    assert first.period_start == now
    assert first.period_end == now + timedelta(days=30)


def test_time_expiry_rolls_without_carrying_unused_quota() -> None:
    now = datetime.now(UTC)
    first = SimpleNamespace(
        id=2,
        plan_id=20,
        status=UsagePeriodStatus.QUEUED,
        period_start=now,
        period_end=now,
        quota_bytes=200,
    )
    subscription, current, db = _rollover_state(
        used=10, end=now - timedelta(seconds=1), queued=[first]
    )
    assert apply_expiry_policy(db, subscription, now) is True
    assert current.status is UsagePeriodStatus.CLOSED
    assert first.status is UsagePeriodStatus.ACTIVE
    assert first.quota_bytes == 200
    assert first.period_start == now
    assert first.period_end == now + timedelta(days=30)


def test_time_expiry_with_empty_queue_enters_grace() -> None:
    now = datetime.now(UTC)
    subscription, current, db = _rollover_state(used=10, end=now - timedelta(seconds=1), queued=[])
    assert apply_expiry_policy(db, subscription, now) is True
    assert current.status is UsagePeriodStatus.CLOSED
    assert subscription.status is SubscriptionStatus.GRACE


def test_quota_exhaustion_with_empty_queue_enters_grace() -> None:
    now = datetime.now(UTC)
    subscription, current, db = _rollover_state(used=100, end=now + timedelta(days=1), queued=[])
    assert apply_expiry_policy(db, subscription, now) is True
    assert current.status is UsagePeriodStatus.CLOSED
    assert subscription.status is SubscriptionStatus.GRACE
    assert subscription.service_expire_at > now


def test_rollover_keeps_subscription_token_hash() -> None:
    now = datetime.now(UTC)
    first = SimpleNamespace(
        id=2,
        plan_id=20,
        status=UsagePeriodStatus.QUEUED,
        period_start=now,
        period_end=now,
        quota_bytes=200,
    )
    subscription, _, db = _rollover_state(used=100, end=now + timedelta(days=1), queued=[first])
    subscription.token_hash = "stable-token-hash"
    apply_expiry_policy(db, subscription, now)
    assert subscription.token_hash == "stable-token-hash"


def test_rollover_accounting_write_failure_is_pending_manual() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(
                customer_no="CUS-S07-FAIL",
                email="s07-fail@example.invalid",
                password_hash="x",
            )
            plan = Plan(
                plan_code="S07-FAIL-PLAN",
                name="S07 failure plan",
                traffic_limit_bytes=100,
                duration_days=30,
                price="10.00",
                currency="CNY",
                route_group_code="S07",
            )
            db.add_all([customer, plan])
            db.flush()
            order = Order(
                order_no="ORD-S07-FAIL",
                customer_id=customer.id,
                plan_id=plan.id,
                client_request_id="s07-fail",
                amount="10.00",
                currency="CNY",
            )
            db.add(order)
            db.flush()
            subscription = Subscription(
                subscription_no="SUB-S07-FAIL",
                customer_id=customer.id,
                plan_id=plan.id,
                order_id=order.id,
                status=SubscriptionStatus.ACTIVE,
                route_group_code="S07",
                service_expire_at=datetime.now(UTC) + timedelta(days=30),
                accounting_user_id="acct-s07-fail",
                current_period_id=None,
            )
            db.add(subscription)
            db.flush()
            period = UsagePeriod(
                subscription_id=subscription.id,
                order_id=order.id,
                plan_id=plan.id,
                period_start=datetime.now(UTC),
                period_end=datetime.now(UTC) + timedelta(days=30),
                quota_bytes=100,
                status=UsagePeriodStatus.ACTIVE,
            )
            db.add(period)
            db.flush()
            subscription.current_period_id = period.id
            db.commit()
            provider = MockAccountingProvider(
                failures={"set_quota": RuntimeError("s07-rollover-write-failed")}
            )
            provider.users["acct-s07-fail"] = provider.create_user(
                "acct-s07-fail", 100, subscription.service_expire_at
            )
            with pytest.raises(RuntimeError, match="s07-rollover-write-failed"):
                reconcile_subscription(
                    db,
                    subscription,
                    provider,
                    pending_manual_on_failure=True,
                )
            db.expire_all()
            persisted = db.scalar(
                select(Subscription).where(Subscription.id == subscription.id)
            )
            assert persisted is not None
            assert persisted.reconcile_state is ReconcileState.PENDING_MANUAL
            assert persisted.provision_error == "s07-rollover-write-failed"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B8: scheduler process lifecycle -- one registry per
# process, reused across every batch, closed deterministically on exit.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CloseTrackingProvider:
    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)


def _spy_registry(egress: _CloseTrackingProvider) -> ProviderRegistry:
    defaults = build_registry(Settings())
    return ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )


_EMPTY_RESULT = SchedulerBatchResult(
    transport_records=0,
    reconciled_subscriptions=0,
    usage_jobs=0,
    fast_usage_poll=False,
    egress_endpoints_checked=0,
    egress_drift_findings=0,
)


def test_main_one_shot_builds_registry_once_and_closes_it_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    egress = _CloseTrackingProvider()
    registry = _spy_registry(egress)
    registries_used: list[object] = []

    def fake_run_batch(
        used_registry: object = None, db_factory: object = None
    ) -> SchedulerBatchResult:
        del db_factory
        assert used_registry is not None
        registries_used.append(used_registry)
        return _EMPTY_RESULT

    monkeypatch.setattr(scheduler, "build_scheduler_registry", lambda: registry)
    monkeypatch.setattr(scheduler, "run_batch", fake_run_batch)
    monkeypatch.setattr("sys.argv", ["scheduler"])

    assert egress.close_calls == []
    scheduler.main()

    assert registries_used == [registry]
    assert len(egress.close_calls) == 1


def test_main_loop_reuses_one_registry_across_multiple_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    egress = _CloseTrackingProvider()
    registry = _spy_registry(egress)
    registries_used: list[object] = []
    call_count = 0

    def fake_run_batch(
        used_registry: object = None, db_factory: object = None
    ) -> SchedulerBatchResult:
        del db_factory
        nonlocal call_count
        call_count += 1
        assert used_registry is not None
        registries_used.append(used_registry)
        return _EMPTY_RESULT

    sleep_calls: list[int] = []

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        if call_count >= 2:
            raise KeyboardInterrupt("stop the loop after two batches")

    monkeypatch.setattr(scheduler, "build_scheduler_registry", lambda: registry)
    monkeypatch.setattr(scheduler, "run_batch", fake_run_batch)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["scheduler", "--loop"])

    with pytest.raises(KeyboardInterrupt):
        scheduler.main()

    assert call_count >= 2
    # Every batch observed the exact same registry instance -- only one
    # ProviderRegistry (and, for a future resource-owning provider, only
    # one httpx.Client) was ever constructed for this whole process.
    assert len(registries_used) == call_count
    assert all(used is registry for used in registries_used)


def test_main_closes_the_registry_even_when_run_batch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    egress = _CloseTrackingProvider()
    registry = _spy_registry(egress)

    def failing_run_batch(
        used_registry: object = None, db_factory: object = None
    ) -> SchedulerBatchResult:
        del db_factory
        assert used_registry is not None
        raise RuntimeError("simulated batch failure")

    monkeypatch.setattr(scheduler, "build_scheduler_registry", lambda: registry)
    monkeypatch.setattr(scheduler, "run_batch", failing_run_batch)
    monkeypatch.setattr("sys.argv", ["scheduler"])

    with pytest.raises(RuntimeError, match="simulated batch failure"):
        scheduler.main()

    assert len(egress.close_calls) == 1
