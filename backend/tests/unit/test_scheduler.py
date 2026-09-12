from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from backend.app.core.config import Settings
from backend.app.providers.registry import ProviderRegistry, build_registry
from backend.app.workers import scheduler
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
