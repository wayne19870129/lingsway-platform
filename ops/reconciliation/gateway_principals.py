"""Human-triggered reconciliation of persisted Xray route principals.

Phase A performs a complete read-only database snapshot and patched Marzban
GET preflight. Phase B uses the existing GatewayRouteBinding named-lock
contract for one conditional, atomic database transaction. This module is
never imported by application startup, deployment, or the scheduler.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.config import Settings, get_settings
from backend.app.core.database import SessionLocal
from backend.app.infra.gateway_route_lock import (
    GatewayRouteBindingLockError,
    gateway_route_binding_write,
)
from backend.app.models import GatewayRouteBinding, Subscription
from backend.app.providers.accounting.marzban import MarzbanAccountingProvider

logger = logging.getLogger(__name__)


class ReconciliationError(RuntimeError):
    """A safe, non-sensitive reason for aborting reconciliation."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class BindingSnapshot:
    id: int
    subscription_id: int
    enabled: bool
    released_at: datetime | None
    gateway_principal: str
    accounting_user_id: str

    def compare_key(self) -> tuple[object, ...]:
        return (
            self.id,
            self.subscription_id,
            self.enabled,
            self.released_at,
            self.gateway_principal,
            self.accounting_user_id,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    run_timestamp: str
    mode: str
    snapshot_row_count: int
    unchanged_count: int
    would_update_count: int
    updated_count: int
    status: str
    reason_code: str


def _active_snapshot(session: Session) -> tuple[BindingSnapshot, ...]:
    rows = session.execute(
        select(GatewayRouteBinding, Subscription)
        .outerjoin(Subscription, Subscription.id == GatewayRouteBinding.subscription_id)
        .where(
            GatewayRouteBinding.enabled.is_(True),
            GatewayRouteBinding.released_at.is_(None),
        )
        .order_by(GatewayRouteBinding.id)
    ).all()
    snapshots: list[BindingSnapshot] = []
    for binding, subscription in rows:
        if subscription is None:
            raise ReconciliationError("subscription_missing")
        accounting_user_id = subscription.accounting_user_id
        if not isinstance(accounting_user_id, str) or not accounting_user_id.strip():
            raise ReconciliationError("accounting_user_id_missing")
        if accounting_user_id != accounting_user_id.strip():
            raise ReconciliationError("accounting_user_id_malformed")
        if not isinstance(binding.gateway_principal, str) or not binding.gateway_principal.strip():
            raise ReconciliationError("gateway_principal_malformed")
        snapshots.append(
            BindingSnapshot(
                id=binding.id,
                subscription_id=binding.subscription_id,
                enabled=binding.enabled,
                released_at=binding.released_at,
                gateway_principal=binding.gateway_principal,
                accounting_user_id=accounting_user_id,
            )
        )
    return tuple(snapshots)


def _phase_a(
    session: Session, lookup: Callable[[str], str]
) -> tuple[tuple[BindingSnapshot, ...], dict[int, str]]:
    snapshots = _active_snapshot(session)
    if len({row.accounting_user_id for row in snapshots}) != len(snapshots):
        raise ReconciliationError("duplicate_accounting_identity")

    # The database read transaction must not remain open while making the
    # external calls. The immutable dataclass snapshot is the handoff.
    session.rollback()
    targets: dict[int, str] = {}
    lookup_failures = 0
    for row in snapshots:
        try:
            target = lookup(row.accounting_user_id)
        except Exception:
            lookup_failures += 1
            continue
        if not isinstance(target, str) or not target.strip():
            raise ReconciliationError("routing_principal_missing")
        if target != target.strip():
            raise ReconciliationError("routing_principal_malformed")
        targets[row.id] = target
    if lookup_failures:
        raise ReconciliationError("marzban_lookup_failed")
    if len(set(targets.values())) != len(targets):
        raise ReconciliationError("duplicate_target_principal")

    old_principal_owner = {row.gateway_principal: row.id for row in snapshots}
    if any(
        target in old_principal_owner and old_principal_owner[target] != row.id
        for row in snapshots
        for target in (targets[row.id],)
    ):
        raise ReconciliationError("target_principal_conflict")
    return snapshots, targets


def _assert_same_snapshot(
    expected: tuple[BindingSnapshot, ...], observed: tuple[BindingSnapshot, ...]
) -> None:
    if [row.compare_key() for row in expected] != [row.compare_key() for row in observed]:
        raise ReconciliationError("snapshot_changed")


def _conditional_update(session: Session, row: BindingSnapshot, target: str) -> int:
    result = session.execute(
        update(GatewayRouteBinding)
        .where(
            GatewayRouteBinding.id == row.id,
            GatewayRouteBinding.enabled.is_(True),
            GatewayRouteBinding.released_at.is_(None),
            GatewayRouteBinding.gateway_principal == row.gateway_principal,
        )
        .values(gateway_principal=target)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _audit(result: ReconciliationResult) -> None:
    # The result contains counts and reason codes only; never add identities,
    # links, credentials, or provider response payloads to this record.
    logger.info("gateway_principal_reconciliation=%s", json.dumps(asdict(result), sort_keys=True))


def reconcile_gateway_principals(
    session: Session, lookup: Callable[[str], str], *, confirm: bool
) -> ReconciliationResult:
    """Run read-only Phase A and, only with ``confirm``, locked Phase B."""

    timestamp = datetime.now(UTC).isoformat()
    mode = "confirmed" if confirm else "dry-run"
    snapshots: tuple[BindingSnapshot, ...] = ()
    targets: dict[int, str] = {}
    try:
        snapshots, targets = _phase_a(session, lookup)
        unchanged = sum(row.gateway_principal == targets[row.id] for row in snapshots)
        would_update = len(snapshots) - unchanged
        if not confirm:
            result = ReconciliationResult(
                timestamp,
                mode,
                len(snapshots),
                unchanged,
                would_update,
                0,
                "success",
                "dry_run_no_write",
            )
            _audit(result)
            return result

        updated = 0
        commit_completed = False
        try:
            with gateway_route_binding_write(session):
                locked_snapshot = _active_snapshot(session)
                _assert_same_snapshot(snapshots, locked_snapshot)
                for row in snapshots:
                    target = targets[row.id]
                    if target == row.gateway_principal:
                        continue
                    if _conditional_update(session, row, target) != 1:
                        raise ReconciliationError("conditional_update_rowcount")
                    updated += 1
                session.commit()
                commit_completed = True
        except IntegrityError:
            if commit_completed:
                result = ReconciliationResult(
                    timestamp,
                    mode,
                    len(snapshots),
                    unchanged,
                    would_update,
                    updated,
                    "committed_with_warning",
                    "lock_release_unverified",
                )
                _audit(result)
                return result
            session.rollback()
            raise ReconciliationError("unique_conflict") from None
        except GatewayRouteBindingLockError:
            if commit_completed:
                result = ReconciliationResult(
                    timestamp,
                    mode,
                    len(snapshots),
                    unchanged,
                    would_update,
                    updated,
                    "committed_with_warning",
                    "lock_release_unverified",
                )
                _audit(result)
                return result
            session.rollback()
            raise ReconciliationError("lock_acquisition_failed") from None
        except ReconciliationError:
            if commit_completed:
                result = ReconciliationResult(
                    timestamp,
                    mode,
                    len(snapshots),
                    unchanged,
                    would_update,
                    updated,
                    "committed_with_warning",
                    "lock_release_unverified",
                )
                _audit(result)
                return result
            session.rollback()
            raise
        except Exception:
            if commit_completed:
                result = ReconciliationResult(
                    timestamp,
                    mode,
                    len(snapshots),
                    unchanged,
                    would_update,
                    updated,
                    "committed_with_warning",
                    "lock_release_unverified",
                )
                _audit(result)
                return result
            session.rollback()
            raise ReconciliationError("db_write_failed") from None

        result = ReconciliationResult(
            timestamp,
            mode,
            len(snapshots),
            unchanged,
            would_update,
            updated,
            "success",
            "committed",
        )
        _audit(result)
        return result
    except ReconciliationError as exc:
        session.rollback()
        result = ReconciliationResult(
            timestamp, mode, len(snapshots), 0, len(targets), 0, "aborted", exc.reason_code
        )
        _audit(result)
        return result
    except Exception:
        session.rollback()
        result = ReconciliationResult(
            timestamp, mode, len(snapshots), 0, len(targets), 0, "aborted", "unexpected_failure"
        )
        _audit(result)
        return result


def _marzban_from_settings(settings: Settings) -> MarzbanAccountingProvider:
    return MarzbanAccountingProvider(
        base_url=settings.marzban_base_url,
        admin_username=settings.marzban_admin_username,
        admin_password=settings.marzban_admin_password,
        default_protocol=settings.marzban_default_protocol,
        default_inbounds_json=settings.marzban_default_inbounds_json,
        verify_tls=settings.marzban_verify_tls,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="perform the locked atomic update; without it, run dry-run preflight only",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.accounting_provider != "marzban":
        print(json.dumps({"status": "aborted", "reason_code": "marzban_provider_required"}))
        return 2

    provider = _marzban_from_settings(settings)
    try:
        with SessionLocal() as session:
            result = reconcile_gateway_principals(
                session, provider.get_routing_principal, confirm=args.confirm
            )
            print(json.dumps(asdict(result), sort_keys=True))
            return 0 if result.status == "success" else 2
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
