from __future__ import annotations

from backend.app.core.config import Settings
from backend.app.providers.registry import build_registry
from backend.app.workers.scheduler import (
    FAST_USAGE_INTERVAL_SECONDS,
    NORMAL_USAGE_INTERVAL_SECONDS,
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
        registry_factory=lambda: build_registry(settings),
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
