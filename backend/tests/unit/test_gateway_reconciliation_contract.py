from __future__ import annotations

import ast
from pathlib import Path

from backend.app.infra.gateway_reconciliation import _payload

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECONCILER = _REPO_ROOT / "backend" / "app" / "infra" / "gateway_reconciliation.py"
_SERVICES = _REPO_ROOT / "backend" / "app" / "services.py"
_ACCOUNTING = _REPO_ROOT / "backend" / "app" / "workers" / "accounting_sync.py"
_SCHEDULER = _REPO_ROOT / "backend" / "app" / "workers" / "scheduler.py"
_MAIN = _REPO_ROOT / "backend" / "app" / "main.py"


def test_gateway_job_payload_is_identifier_only() -> None:
    payload = _payload(
        operation_kind="PURCHASE",
        subscription_id=7,
        order_id=8,
        provision_run_id="9",
    )
    assert payload == {
        "operation_kind": "PURCHASE",
        "subscription_id": 7,
        "order_id": 8,
        "provision_run_id": "9",
    }
    assert not {"token", "password", "privateKey", "shortIds", "candidate"} & set(payload)


def test_reconciler_uses_existing_job_retry_fields_and_fresh_state() -> None:
    source = _RECONCILER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    text = source
    assert "GATEWAY_RECONCILE_JOB_TYPE = " in text
    assert "Job.attempts < Job.max_attempts" in text
    assert "Job.locked_at < stale_before" in text
    assert "full_desired_routing_snapshot(db)" in text
    assert "SqlAlchemyCredentialResolver(db)" in text
    assert "gateway_route_binding_write(db)" in text
    assert "gateway.render(desired, resolver)" in text
    assert "gateway.apply(candidate)" in text
    assert "select(Job.id)" in text
    assert ".with_for_update()" in text
    assert "classify_first_commit_outcome" in text
    assert "classify_finalization_outcome" in text
    assert "FinalizationCommitOutcome" in text
    assert "GATEWAY_FINALIZATION_COMMIT_OUTCOME_UNKNOWN" in text
    assert "_independent_engine" in text
    assert "result.applied and result.finalized" in text
    assert "reconcile_state" not in text
    assert tree.body


def test_paid_and_release_writers_enqueue_before_runtime_reconcile() -> None:
    services = _SERVICES.read_text(encoding="utf-8")
    accounting = _ACCOUNTING.read_text(encoding="utf-8")
    assert services.index("enqueue_gateway_reconciliation") < services.index(
        "reconcile_gateway_job_in_session"
    )
    assert accounting.index("enqueue_gateway_reconciliation") < accounting.index(
        "reconcile_gateway_job_in_session"
    )
    assert "release_egress(db, locked.id, now, gateway=gateway)" in accounting
    assert "gateway_reconciliation_required" in (
        _REPO_ROOT / "ops" / "reconciliation" / "gateway_principals.py"
    ).read_text(encoding="utf-8")


def test_scheduler_never_executes_real_xray_runtime() -> None:
    source = _SCHEDULER.read_text(encoding="utf-8")
    assert "reconcile_gateway_job" not in source
    assert (
        'runtime_gateway = None if settings.gateway_provider == "xray_file" '
        "else registry.gateway"
    ) in source
    assert "gateway=runtime_gateway" in source


def test_recovery_loop_logs_only_safe_exception_type() -> None:
    source = _MAIN.read_text(encoding="utf-8")
    assert "logger.warning(" in source
    assert "type(exc).__name__" in source
    assert "str(exc)" not in source
    assert ".cancel()" not in source
    assert "await recovery_task" in source
    assert source.index("await recovery_task") < source.index("registry.close()")
