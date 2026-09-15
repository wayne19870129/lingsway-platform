from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from backend.app.infra.gateway_route_lock import GatewayRouteBindingLockError
from ops.reconciliation import gateway_principals as reconciliation


class _Result:
    def __init__(self, rowcount: int = 0, rows: list[tuple[Any, Any]] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def all(self) -> list[tuple[Any, Any]]:
        return self._rows


class _Session:
    def __init__(
        self,
        rowcounts: list[int] | None = None,
        rows: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.rowcounts = list(rowcounts or [])
        self.rows = rows or []
        self.execute_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Result:
        self.execute_calls += 1
        self.statements.append(statement)
        if self.rows:
            return _Result(rows=self.rows)
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        return _Result(rowcount=rowcount)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


def _row(row_id: int, old: str, username: str = "alice") -> reconciliation.BindingSnapshot:
    return reconciliation.BindingSnapshot(
        id=row_id,
        subscription_id=row_id,
        enabled=True,
        released_at=None,
        gateway_principal=old,
        accounting_user_id=username,
    )


@contextmanager
def _lock(_session: Any) -> Iterator[None]:
    yield


def _sequence(
    monkeypatch: pytest.MonkeyPatch,
    *snapshots: tuple[reconciliation.BindingSnapshot, ...],
) -> None:
    values = iter(snapshots)
    monkeypatch.setattr(reconciliation, "_active_snapshot", lambda _session: next(values))
    monkeypatch.setattr(reconciliation, "gateway_route_binding_write", _lock)


def test_phase_a_success_then_phase_b_commits_conditional_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = (_row(1, "principal-a"), _row(2, "principal-b", "bob"))
    _sequence(monkeypatch, first, first)
    session = _Session(rowcounts=[1])

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session),
        lambda username: {"alice": "principal-a", "bob": "principal-b-new"}[username],
        confirm=True,
    )

    assert result.status == "success"
    assert result.updated_count == 1
    assert result.unchanged_count == 1
    assert session.execute_calls == 1
    assert session.commit_calls == 1


def test_any_marzban_lookup_failure_causes_zero_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_row(1, "principal-a"), _row(2, "principal-b", "bob"))
    _sequence(monkeypatch, first)
    session = _Session()
    calls: list[str] = []

    def lookup(username: str) -> str:
        calls.append(username)
        if username == "bob":
            raise RuntimeError("transport failure")
        return "principal-a"

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lookup, confirm=True
    )

    assert result.reason_code == "marzban_lookup_failed"
    assert calls == ["alice", "bob"]
    assert session.execute_calls == 0
    assert session.commit_calls == 0


@pytest.mark.parametrize("target", ["", "   "])
def test_missing_or_blank_routing_principal_causes_zero_writes(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    first = (_row(1, "principal-a"),)
    _sequence(monkeypatch, first)
    session = _Session()

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: target, confirm=True
    )

    assert result.status == "aborted"
    assert result.reason_code == "routing_principal_missing"
    assert session.execute_calls == 0


def test_duplicate_target_principal_causes_zero_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_row(1, "principal-a"), _row(2, "principal-b", "bob"))
    _sequence(monkeypatch, first)
    session = _Session()

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: "same", confirm=True
    )

    assert result.reason_code == "duplicate_target_principal"
    assert session.execute_calls == 0


def test_snapshot_change_after_phase_a_aborts_before_write(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_row(1, "principal-a"),)
    changed = (_row(1, "principal-changed"),)
    _sequence(monkeypatch, first, changed)
    session = _Session(rowcounts=[1])

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: "principal-new", confirm=True
    )

    assert result.reason_code == "snapshot_changed"
    assert session.execute_calls == 0
    assert session.commit_calls == 0


def test_conditional_update_rowcount_failure_rolls_back_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = (_row(1, "principal-a"),)
    _sequence(monkeypatch, first, first)
    session = _Session(rowcounts=[0])

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: "principal-new", confirm=True
    )

    assert result.reason_code == "conditional_update_rowcount"
    assert session.commit_calls == 0
    assert session.rollback_calls >= 1


def test_lock_acquisition_failure_causes_zero_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_row(1, "principal-a"),)
    monkeypatch.setattr(reconciliation, "_active_snapshot", lambda _session: first)

    @contextmanager
    def fail_lock(_session: Any) -> Iterator[None]:
        raise GatewayRouteBindingLockError("lock unavailable")
        yield

    monkeypatch.setattr(reconciliation, "gateway_route_binding_write", fail_lock)
    session = _Session()

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: "principal-new", confirm=True
    )

    assert result.reason_code == "lock_acquisition_failed"
    assert session.execute_calls == 0


def test_second_already_reconciled_run_is_a_safe_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_row(1, "principal-a"),)
    _sequence(monkeypatch, first, first)
    session = _Session()

    result = reconciliation.reconcile_gateway_principals(
        cast(Session, session), lambda _username: "principal-a", confirm=True
    )

    assert result.status == "success"
    assert result.reason_code == "committed"
    assert result.unchanged_count == 1
    assert result.would_update_count == 0
    assert result.updated_count == 0
    assert session.execute_calls == 0


def test_dry_run_never_writes_and_audit_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = (_row(1, "principal-a"),)
    _sequence(monkeypatch, first)
    session = _Session()
    secret = "admin-password-token-privateKey-shortIds"

    with caplog.at_level("INFO"):
        result = reconciliation.reconcile_gateway_principals(
            cast(Session, session), lambda _username: "principal-new", confirm=False
        )

    assert result.mode == "dry-run"
    assert result.reason_code == "dry_run_no_write"
    assert session.execute_calls == 0
    assert session.commit_calls == 0
    assert secret not in caplog.text


def test_active_snapshot_query_excludes_disabled_and_released_rows() -> None:
    session = _Session()

    assert reconciliation._active_snapshot(cast(Session, session)) == ()
    compiled = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "enabled IS true" in compiled
    assert "released_at IS NULL" in compiled


def test_missing_subscription_is_fail_closed() -> None:
    binding = SimpleNamespace(
        id=1,
        subscription_id=1,
        enabled=True,
        released_at=None,
        gateway_principal="principal-a",
    )
    session = _Session(rows=[(binding, None)])

    with pytest.raises(reconciliation.ReconciliationError) as raised:
        reconciliation._active_snapshot(cast(Session, session))

    assert raised.value.reason_code == "subscription_missing"
