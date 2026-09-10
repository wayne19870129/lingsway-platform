"""Detect egress IP drift between the database and the provider's live inventory.

This is read-only: it never mutates EgressEndpoint state and never calls
replace_endpoint. Findings are written to AuditLog for a human to review and
act on manually (see TASK-T5G-scheduler-jobs.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import AuditLog, EgressEndpoint
from backend.app.providers.base import EgressProvider

ACTIVE_ENDPOINT_STATUSES = ("AVAILABLE", "ASSIGNED")


@dataclass(frozen=True, slots=True)
class DriftFinding:
    endpoint_code: str
    db_host: str
    db_port: int
    provider_host: str
    provider_port: int


@dataclass(frozen=True, slots=True)
class DriftCheckResult:
    checked: int
    findings: tuple[DriftFinding, ...]
    provider_returned_no_data: bool


def check_egress_drift(db: Session, provider: EgressProvider) -> DriftCheckResult:
    """Compare DB-recorded host/port for active endpoints against the
    provider's live `list_endpoints()`.

    Matching is by `EgressEndpoint.code` == `EgressEndpointDTO.endpoint_id`;
    a provider implementation must key its DTOs that way for drift checking
    to find them (the current MockEgressProvider already does this).

    A provider returning an empty list is treated as "could not verify",
    not "every endpoint drifted" -- some provider implementations (e.g. the
    current `providers/egress/webshare.py`, whose `list_endpoints()` is
    still a stub returning `[]` pending full response-mapping work) can
    legitimately return nothing without every endpoint actually being gone.
    Raising one finding per DB endpoint in that case would be a false-
    positive alert storm, so a single "no data" AuditLog entry is recorded
    instead.
    """
    endpoints = db.scalars(
        select(EgressEndpoint).where(EgressEndpoint.status.in_(ACTIVE_ENDPOINT_STATUSES))
    ).all()
    if not endpoints:
        return DriftCheckResult(checked=0, findings=(), provider_returned_no_data=False)

    provider_by_code = {dto.endpoint_id: dto for dto in provider.list_endpoints()}

    if not provider_by_code:
        _record_no_data(db, len(endpoints))
        return DriftCheckResult(
            checked=len(endpoints), findings=(), provider_returned_no_data=True
        )

    findings: list[DriftFinding] = []
    for endpoint in endpoints:
        dto = provider_by_code.get(endpoint.code)
        if dto is None:
            # Not reported by the provider this round; could be a different
            # provider's endpoint, or a naming mismatch -- not evidence of
            # drift on its own, so it is not flagged here.
            continue
        if dto.host != endpoint.host or dto.port != endpoint.port:
            findings.append(
                DriftFinding(endpoint.code, endpoint.host, endpoint.port, dto.host, dto.port)
            )

    for finding in findings:
        _record_drift(db, finding)
    if findings:
        db.commit()

    return DriftCheckResult(
        checked=len(endpoints), findings=tuple(findings), provider_returned_no_data=False
    )


def _record_no_data(db: Session, expected_count: int) -> None:
    db.add(
        AuditLog(
            action="EGRESS_DRIFT_CHECK_NO_DATA",
            entity_type="EGRESS_PROVIDER",
            entity_id="*",
            result="FAILURE",
            detail=(
                f"list_endpoints() returned no data while {expected_count} DB "
                "endpoint(s) were expected; drift could not be verified this round."
            ),
        )
    )
    db.commit()


def _record_drift(db: Session, finding: DriftFinding) -> None:
    db.add(
        AuditLog(
            action="EGRESS_DRIFT_DETECTED",
            entity_type="EGRESS_ENDPOINT",
            entity_id=finding.endpoint_code,
            result="FAILURE",
            detail=(
                f"DB host/port {finding.db_host}:{finding.db_port} does not match "
                f"provider-reported {finding.provider_host}:{finding.provider_port}; "
                "requires manual review, no automatic correction performed."
            ),
        )
    )
