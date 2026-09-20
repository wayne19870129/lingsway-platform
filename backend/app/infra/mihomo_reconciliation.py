from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import (
    MIHOMO_CONTROLLER_SECRET_REF,
    SqlMihomoControllerSecretResolver,
)
from backend.app.infra.mihomo_blocker import (
    MIHOMO_BLOCKER_KIND_FINALIZATION,
    MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
    MIHOMO_LOCK_RELEASE_PENDING,
    MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
    ensure_mihomo_blocker,
    mihomo_blocker_dedupe_key,
    validate_mihomo_operation_id,
)
from backend.app.infra.mihomo_generation import (
    allocate_generation,
    build_projection_source_manifest,
    snapshot_identity,
)
from backend.app.infra.mihomo_materialization import (
    load_transport_materialization,
    verify_materialization,
)
from backend.app.infra.mihomo_projection_lock import (
    SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY,
    SESSION_INFO_RELEASE_METADATA_KEY,
    mihomo_projection_write,
    session_holds_mihomo_projection_lock,
)
from backend.app.models import (
    EgressEndpoint,
    EgressGroup,
    EgressTransportAssignment,
    Job,
    JobStatus,
    MihomoTransportMaterialization,
    RouteBinding,
    RouteEgressBinding,
    RouteGroup,
    RouteGroupStatus,
    Secret,
    TrafficRule,
    TransportEndpointRecord,
    TransportProviderRecord,
)
from backend.app.providers.base import DesiredForwarderState, ForwarderListenerDTO
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretResolver,
    MihomoApplyError,
    MihomoRollbackUnknownError,
    MihomoRollbackVerifiedError,
)
from backend.app.providers.forwarder.mihomo_projection import (
    MihomoProjectionError,
    TransportMaterializationReference,
)
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
)

MIHOMO_RECONCILE_JOB_TYPE = "MIHOMO_RECONCILE"
MIHOMO_OPERATION_KINDS = frozenset(
    {"FULL_PROJECTION", "RECONCILE", "CREATE", "UPDATE", "DELETE"}
)
MIHOMO_FINALIZATION_UNKNOWN = "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
MIHOMO_RUNTIME_UNKNOWN = "MIHOMO_RUNTIME_PROJECTION_UNKNOWN"
MIHOMO_ROLLBACK_UNKNOWN = "MIHOMO_ROLLBACK_OUTCOME_UNKNOWN"
MIHOMO_UNKNOWN_DIAGNOSTICS = frozenset(
    {MIHOMO_FINALIZATION_UNKNOWN, MIHOMO_RUNTIME_UNKNOWN, MIHOMO_ROLLBACK_UNKNOWN}
)
_OPERATION_ID_PREFIX = "MIHOMO_RECONCILE:"
_MAX_OPERATION_ID_LENGTH = 160 - len(_OPERATION_ID_PREFIX)
_RETRY_LIMIT = 5
_STALE_AFTER = timedelta(minutes=15)


class MihomoReconciliationError(RuntimeError):
    pass


class MihomoPayloadError(MihomoReconciliationError):
    pass


class MihomoReconciliationBlocked(MihomoReconciliationError):
    pass


class MihomoCommitOutcomeUnknown(MihomoReconciliationError):
    pass


class CommitOutcome(StrEnum):
    LANDED = "LANDED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProjectionVerification:
    verified: bool
    evidence_version: str


class MihomoDesiredSnapshotLoader(Protocol):
    def load(self, db: Session, job: Job) -> DesiredForwarderState: ...


class SqlMihomoDesiredSnapshotLoader:
    """Rebuild the complete desired projection from committed DB authority."""

    def __init__(self, cache_root: Path, external_controller: str) -> None:
        self._cache_root = cache_root
        self._external_controller = external_controller

    def load(self, db: Session, job: Job) -> DesiredForwarderState:
        del job
        groups = list(
            db.scalars(
                select(RouteGroup)
                .where(RouteGroup.status == RouteGroupStatus.ACTIVE)
                .order_by(RouteGroup.code, RouteGroup.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        group_ids = {group.id for group in groups}
        bindings = list(
            db.scalars(
                select(RouteBinding)
                .where(
                    RouteBinding.enabled.is_(True),
                    RouteBinding.route_group_id.in_(group_ids or {-1}),
                )
                .order_by(RouteBinding.route_group_id, RouteBinding.priority, RouteBinding.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        egress_bindings = list(
            db.scalars(
                select(RouteEgressBinding)
                .where(
                    RouteEgressBinding.enabled.is_(True),
                    RouteEgressBinding.route_group_id.in_(group_ids or {-1}),
                )
                .order_by(RouteEgressBinding.route_group_id, RouteEgressBinding.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        rules = list(
            db.scalars(
                select(TrafficRule)
                .where(TrafficRule.enabled.is_(True))
                .order_by(TrafficRule.priority, TrafficRule.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        egress_groups = list(
            db.scalars(
                select(EgressGroup)
                .where(EgressGroup.status == "ACTIVE")
                .order_by(EgressGroup.code, EgressGroup.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        egress_group_ids = {group.id for group in egress_groups}
        endpoints = list(
            db.scalars(
                select(EgressEndpoint)
                .where(
                    EgressEndpoint.status == "AVAILABLE",
                    EgressEndpoint.group_id.in_(egress_group_ids or {-1}),
                )
                .order_by(EgressEndpoint.code, EgressEndpoint.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        provider_ids = {binding.provider_id for binding in bindings}
        providers = list(
            db.scalars(
                select(TransportProviderRecord)
                .where(
                    TransportProviderRecord.enabled.is_(True),
                    TransportProviderRecord.id.in_(provider_ids or {-1}),
                )
                .order_by(TransportProviderRecord.code, TransportProviderRecord.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        provider_id_set = {provider.id for provider in providers}
        transport_endpoints = tuple(
            db.scalars(
                select(TransportEndpointRecord)
                .where(
                    TransportEndpointRecord.provider_id.in_(provider_id_set or {-1}),
                    TransportEndpointRecord.status == "AVAILABLE",
                )
                .order_by(
                    TransportEndpointRecord.provider_id,
                    TransportEndpointRecord.external_id,
                    TransportEndpointRecord.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        assignments = tuple(
            db.scalars(
                select(EgressTransportAssignment)
                .where(
                    EgressTransportAssignment.transport_provider_id.in_(provider_id_set or {-1}),
                    EgressTransportAssignment.state == "ACTIVE",
                )
                .order_by(
                    EgressTransportAssignment.transport_provider_id,
                    EgressTransportAssignment.transport_node_id,
                    EgressTransportAssignment.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        del transport_endpoints, assignments
        receipts = {
            receipt.owner_record_id: receipt
            for receipt in db.scalars(
                select(MihomoTransportMaterialization)
                .where(MihomoTransportMaterialization.owner_record_id.in_(provider_id_set or {-1}))
                .order_by(MihomoTransportMaterialization.owner_record_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        materials = []
        references = []
        resolver = SubscriptionTransportResolver(self._cache_root)
        try:
            for provider in providers:
                receipt = receipts.get(provider.id)
                if receipt is None:
                    raise MihomoReconciliationError("MIHOMO_MATERIALIZATION_RECEIPT_MISSING")
                source = db.scalar(
                    select(Secret)
                    .where(
                        Secret.secret_ref == provider.secret_ref,
                        Secret.purpose == "TRANSPORT_SUBSCRIPTION_URL",
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if source is None or source.revision != receipt.source_revision:
                    raise MihomoReconciliationError(
                        "MIHOMO_MATERIALIZATION_SOURCE_REVISION_MISMATCH"
                    )
                descriptor = TransportProviderDescriptor(
                    provider.id,
                    provider.code,
                    provider.kind.value,
                    provider.secret_ref,
                )
                cache_path = resolver._cache_path(descriptor)  # noqa: SLF001
                verified = verify_materialization(
                    db,
                    provider.id,
                    cache_path,
                    provider.code,
                    source_revision=receipt.source_revision,
                )
                material = load_transport_materialization(verified)
                materials.append(material)
                references.append(
                    TransportMaterializationReference(
                        provider.id,
                        provider.code,
                        receipt.source_revision,
                        receipt.cache_identity,
                    )
                )
        finally:
            resolver.close()
        proxies = tuple(
            {
                "name": endpoint.code,
                "type": endpoint.protocol,
                "server": endpoint.host,
                "port": endpoint.port,
            }
            for endpoint in endpoints
        )
        endpoint_names = {endpoint.id: endpoint.code for endpoint in endpoints}
        groups_by_id = {group.id: group.code for group in groups}
        proxy_groups = tuple(
            {
                "name": groups_by_id[group_id],
                "type": "select",
                "proxies": tuple(
                    endpoint_names[binding.egress_endpoint_id]
                    for binding in egress_bindings
                    if binding.route_group_id == group_id
                    and binding.egress_endpoint_id in endpoint_names
                ),
            }
            for group_id in sorted(groups_by_id)
        )
        listeners = tuple(
            ForwarderListenerDTO(
                f"listener-{endpoint.code}",
                "mixed",
                "127.0.0.1",
                endpoint.mihomo_listen_port,
                endpoint.code,
            )
            for endpoint in endpoints
        )
        desired_rules = tuple(
            {
                "match": rule.match_type,
                **({"value": rule.match_value} if rule.match_type != "MATCH" else {}),
                "target": rule.target_egress,
            }
            for rule in rules
        )
        if not desired_rules or desired_rules[-1].get("match") != "MATCH":
            desired_rules = (*desired_rules, {"match": "MATCH", "target": "BLOCK"})
        controller = SqlMihomoControllerSecretResolver(db).resolve(MIHOMO_CONTROLLER_SECRET_REF)
        constants = {
            "external-controller": self._external_controller,
            "api-secret-ref": MIHOMO_CONTROLLER_SECRET_REF,
            "api-secret-revision": controller.revision,
        }
        return DesiredForwarderState(
            listener_specs=listeners,
            proxies=proxies,
            proxy_groups=proxy_groups,
            rules=desired_rules,
            dns={"enable": True, "ipv6": False},
            transport_materializations=tuple(materials),
            transport_references=tuple(references),
            deployment_constants=constants,
        )


class ProjectionVerifier(Protocol):
    def verify(
        self, candidate: object, *, operation_id: str, snapshot_revision: int
    ) -> ProjectionVerification: ...


class ProjectionCandidate(Protocol):
    @property
    def version(self) -> str: ...


class MihomoProjectionProvider(Protocol):
    def render(self, desired: DesiredForwarderState) -> object: ...

    def finalize(
        self, template: object, resolver: ControllerSecretResolver
    ) -> ProjectionCandidate: ...

    def apply(self, candidate: ProjectionCandidate) -> object: ...


@dataclass(frozen=True, slots=True)
class MihomoReconciliationResult:
    job_id: int
    applied: bool
    finalized: bool
    retryable: bool
    reason_code: str | None = None


def prepare_mihomo_reconciliation(
    db: Session,
    desired: DesiredForwarderState,
    *,
    operation_id: str,
    operation_kind: str = "RECONCILE",
) -> Job:
    """Persist one generation and its identifier-only intent atomically."""
    with mihomo_projection_write(db):
        manifest = build_projection_source_manifest(desired)
        generation = allocate_generation(db, manifest)
        snapshot_identity(generation.revision, generation.desired_fingerprint)
        job = enqueue_mihomo_reconciliation(
            db,
            operation_id=operation_id,
            operation_kind=operation_kind,
            snapshot_revision=generation.revision,
        )
        db.commit()
        return job


def _validate_operation_id(value: object) -> str:
    if isinstance(value, str) and len(value) > _MAX_OPERATION_ID_LENGTH:
        raise ValueError("operation_id must be a non-empty canonical string")
    return validate_mihomo_operation_id(value)


def _validate_operation_kind(value: object) -> str:
    if not isinstance(value, str) or value not in MIHOMO_OPERATION_KINDS:
        raise ValueError("operation_kind is not supported")
    return value


def _validate_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("snapshot_revision must be a positive integer")
    return value


def _valid_evidence_version(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value
    ) is not None


def _payload(job: Job) -> dict[str, object]:
    try:
        value = json.loads(job.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MihomoPayloadError("MIHOMO_RECONCILIATION_PAYLOAD_INVALID") from exc
    if not isinstance(value, dict):
        raise MihomoPayloadError("MIHOMO_RECONCILIATION_PAYLOAD_INVALID")
    return value


def enqueue_mihomo_reconciliation(
    db: Session, *, operation_id: str, operation_kind: str, snapshot_revision: int
) -> Job:
    operation_id = _validate_operation_id(operation_id)
    operation_kind = _validate_operation_kind(operation_kind)
    revision = _validate_revision(snapshot_revision)
    key = f"{_OPERATION_ID_PREFIX}{operation_id}"
    existing = db.scalar(select(Job).where(Job.dedupe_key == key).with_for_update())
    if existing is not None:
        try:
            payload = _payload(existing)
            identity_matches = (
                existing.job_type == MIHOMO_RECONCILE_JOB_TYPE
                and payload.get("operation_id") == operation_id
                and payload.get("operation_kind") == operation_kind
                and payload.get("snapshot_revision") == revision
            )
        except MihomoReconciliationError:
            identity_matches = False
        if not identity_matches:
            raise MihomoReconciliationError("MIHOMO_RECONCILIATION_IDENTITY_MISMATCH")
        return existing
    job = Job(
        job_type=MIHOMO_RECONCILE_JOB_TYPE,
        dedupe_key=key,
        payload_json=json.dumps(
            {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "snapshot_revision": revision,
            },
            sort_keys=True,
        ),
        status=JobStatus.PENDING,
        attempts=0,
        max_attempts=_RETRY_LIMIT,
        available_at=datetime.now(UTC),
    )
    db.add(job)
    db.flush()
    return job


def assert_no_unresolved_mihomo_mutation(
    db: Session, *, owning_job_id: int | None = None
) -> None:
    if not session_holds_mihomo_projection_lock(db):
        raise MihomoReconciliationBlocked("MIHOMO_PROJECTION_LOCK_REQUIRED")
    blocker = db.scalar(
        select(Job)
        .where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.status != JobStatus.SUCCEEDED,
        )
        .order_by(Job.id)
        .limit(1)
        .with_for_update()
    )
    if blocker is not None:
        raise MihomoReconciliationBlocked("MIHOMO_GLOBAL_MANUAL_BLOCKER")
    filters = [
        Job.job_type == MIHOMO_RECONCILE_JOB_TYPE,
        Job.status != JobStatus.SUCCEEDED,
    ]
    if owning_job_id is not None:
        filters.append(Job.id < owning_job_id)
    row = db.scalar(
        select(Job).where(*filters).order_by(Job.id).limit(1).with_for_update()
    )
    if row is not None:
        raise MihomoReconciliationBlocked("MIHOMO_UNRESOLVED_INTENT_BLOCKS_WRITER")


def _engine(db: Session) -> Engine:
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    if not isinstance(engine, Engine):
        raise MihomoReconciliationError("MIHOMO_COMMIT_OBSERVER_UNAVAILABLE")
    return engine


def _has_exact_unresolved_release_blocker(
    db: Session, *, job_id: int, operation_id: str, snapshot_revision: int
) -> bool:
    blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == mihomo_blocker_dedupe_key(
                MIHOMO_BLOCKER_KIND_LOCK_RELEASE, job_id
            ),
        )
    )
    if blocker is None or blocker.status is JobStatus.SUCCEEDED:
        return False
    try:
        payload = json.loads(blocker.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(payload == {
        "blocker_kind": MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
        "source_job_id": job_id,
        "operation_id": operation_id,
        "snapshot_revision": snapshot_revision,
        "reason_code": MIHOMO_LOCK_RELEASE_PENDING,
    })


def classify_finalization_outcome(
    db: Session,
    *,
    job_id: int,
    operation_id: str,
    snapshot_revision: int,
    candidate_fingerprint: str,
    operation_kind: str,
) -> CommitOutcome:
    """Classify using a fresh independent observer; ambiguity is never ABSENT."""
    try:
        with Session(bind=_engine(db), autoflush=False, expire_on_commit=False) as observer:
            job = observer.get(Job, job_id)
            if job is None or job.job_type != MIHOMO_RECONCILE_JOB_TYPE:
                return CommitOutcome.UNKNOWN
            payload = _payload(job)
            if (
                payload.get("operation_id") != operation_id
                or payload.get("snapshot_revision") != snapshot_revision
                or payload.get("operation_kind") != operation_kind
            ):
                return CommitOutcome.UNKNOWN
            evidence = payload.get("finalization_evidence")
            if job.status is JobStatus.SUCCEEDED:
                if not isinstance(evidence, dict):
                    return CommitOutcome.UNKNOWN
                if (
                    evidence.get("operation_id") == operation_id
                    and evidence.get("operation_kind") == payload.get("operation_kind")
                    and evidence.get("snapshot_revision") == snapshot_revision
                    and evidence.get("candidate_fingerprint") == candidate_fingerprint
                    and _valid_evidence_version(evidence.get("verification"))
                    and _has_exact_unresolved_release_blocker(
                        observer,
                        job_id=job_id,
                        operation_id=operation_id,
                        snapshot_revision=snapshot_revision,
                    )
                ):
                    return CommitOutcome.LANDED
                return CommitOutcome.UNKNOWN
            if (
                job.status is JobStatus.RUNNING
                and evidence is None
                and job.last_error_code not in MIHOMO_UNKNOWN_DIAGNOSTICS
            ):
                return CommitOutcome.ABSENT
            return CommitOutcome.UNKNOWN
    except Exception:
        return CommitOutcome.UNKNOWN


def _claim(db: Session, now: datetime) -> Job | None:
    stale = now - _STALE_AFTER
    job = db.scalar(
        select(Job)
        .where(
            Job.job_type == MIHOMO_RECONCILE_JOB_TYPE,
            or_(
                Job.status.in_([JobStatus.PENDING, JobStatus.FAILED]),
                (Job.status == JobStatus.RUNNING)
                & (Job.locked_at < stale)
                & or_(
                    Job.last_error_code.is_(None),
                    ~Job.last_error_code.in_(MIHOMO_UNKNOWN_DIAGNOSTICS),
                ),
            ),
            Job.available_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status, job.locked_at = JobStatus.RUNNING, now
    job.attempts += 1
    db.commit()
    return job


def _failure(
    db: Session, job_id: int, error: BaseException, now: datetime
) -> MihomoReconciliationResult:
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        raise MihomoReconciliationError("MIHOMO_RECONCILIATION_JOB_MISSING") from error
    exhausted = job.attempts >= job.max_attempts
    code = (
        str(error)
        if isinstance(
            error, (MihomoReconciliationError, MihomoApplyError, MihomoProjectionError)
        )
        else "MIHOMO_RECONCILIATION_RUNTIME_FAILURE"
    )
    job.status, job.locked_at, job.last_error_code = JobStatus.FAILED, None, code[:80]
    job.available_at = now + timedelta(minutes=min(2 ** max(job.attempts, 1), 60))
    db.commit()
    return MihomoReconciliationResult(job_id, False, False, not exhausted, code[:80])


def _landed_result(job_id: int, *, applied: bool) -> MihomoReconciliationResult:
    return MihomoReconciliationResult(job_id, applied, True, False)


def _persist_unknown_blocker(
    db: Session,
    job_id: int,
    *,
    operation_id: str | None,
    snapshot_revision: int | None,
    diagnostic: str,
    applied: bool,
) -> MihomoReconciliationResult:
    try:
        ensure_mihomo_blocker(
            db,
            blocker_kind=MIHOMO_BLOCKER_KIND_FINALIZATION,
            source_job_id=job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
            reason_code=diagnostic,
        )
        db.commit()
    except Exception as exc:
        db.info[SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY] = True
        with suppress(Exception):
            db.rollback()
        raise MihomoCommitOutcomeUnknown(diagnostic) from exc
    return MihomoReconciliationResult(job_id, applied, False, False, diagnostic)


def _mark_unknown_inner(
    db: Session,
    job_id: int,
    *,
    operation_id: str,
    operation_kind: str,
    snapshot_revision: int,
    candidate_fingerprint: str,
    diagnostic: str,
    applied: bool = True,
    persist_blocker: bool = False,
) -> MihomoReconciliationResult:
    """Persist uncertainty only after a fresh exact read proves it is safe.

    A failed acknowledgement can race with the durable commit.  The
    independent observer is therefore authoritative: a later exact SUCCEEDED
    row is LANDED, an exact RUNNING/no-evidence row may receive the manual
    marker, and every other state is preserved without guessed mutation.
    """
    outcome = classify_finalization_outcome(
        db,
        job_id=job_id,
        operation_id=operation_id,
        operation_kind=operation_kind,
        snapshot_revision=snapshot_revision,
        candidate_fingerprint=candidate_fingerprint,
    )
    if outcome is CommitOutcome.LANDED:
        return _landed_result(job_id, applied=applied)

    db.rollback()
    current = db.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        return _persist_unknown_blocker(
            db,
            job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
            diagnostic=diagnostic,
            applied=applied,
        )
    try:
        current_payload = _payload(current)
    except MihomoPayloadError:
        return _persist_unknown_blocker(
            db,
            job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
            diagnostic=diagnostic,
            applied=applied,
        )
    exact_identity = (
        current.job_type == MIHOMO_RECONCILE_JOB_TYPE
        and current_payload.get("operation_id") == operation_id
        and current_payload.get("operation_kind") == operation_kind
        and current_payload.get("snapshot_revision") == snapshot_revision
    )
    evidence = current_payload.get("finalization_evidence")
    if (
        current.status is JobStatus.SUCCEEDED
        and exact_identity
        and isinstance(evidence, dict)
        and evidence.get("operation_id") == operation_id
        and evidence.get("operation_kind") == operation_kind
        and evidence.get("snapshot_revision") == snapshot_revision
        and evidence.get("candidate_fingerprint") == candidate_fingerprint
        and _valid_evidence_version(evidence.get("verification"))
        and _has_exact_unresolved_release_blocker(
            db,
            job_id=job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
        )
    ):
        return _landed_result(job_id, applied=applied)
    if (
        current.status is not JobStatus.RUNNING
        or not exact_identity
        or evidence is not None
        or current.last_error_code in MIHOMO_UNKNOWN_DIAGNOSTICS
    ):
        return _persist_unknown_blocker(
            db,
            job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
            diagnostic=diagnostic,
            applied=applied,
        )
    current.status = JobStatus.RUNNING
    current.locked_at = datetime.now(UTC)
    current.last_error_code = diagnostic
    if persist_blocker:
        return _persist_unknown_blocker(
            db,
            job_id,
            operation_id=operation_id,
            snapshot_revision=snapshot_revision,
            diagnostic=diagnostic,
            applied=applied,
        )
    try:
        db.commit()
    except Exception as exc:
        raise MihomoCommitOutcomeUnknown(diagnostic) from exc
    return MihomoReconciliationResult(job_id, applied, False, False, diagnostic)


def _mark_unknown(
    db: Session,
    job_id: int,
    *,
    operation_id: str,
    operation_kind: str,
    snapshot_revision: int,
    candidate_fingerprint: str,
    diagnostic: str,
    applied: bool = True,
    persist_blocker: bool = False,
) -> MihomoReconciliationResult:
    try:
        return _mark_unknown_inner(
            db,
            job_id,
            operation_id=operation_id,
            operation_kind=operation_kind,
            snapshot_revision=snapshot_revision,
            candidate_fingerprint=candidate_fingerprint,
            diagnostic=diagnostic,
            applied=applied,
            persist_blocker=persist_blocker,
        )
    except MihomoCommitOutcomeUnknown:
        db.info[SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY] = True
        raise
    except Exception as exc:
        db.info[SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY] = True
        raise MihomoCommitOutcomeUnknown(diagnostic) from exc


def _reset_blocked_claim(db: Session, job: Job) -> None:
    db.rollback()
    current = db.get(Job, job.id)
    if current is not None and current.status is JobStatus.RUNNING:
        current.status = JobStatus.PENDING
        current.locked_at = None
        current.attempts = max(current.attempts - 1, 0)
        db.commit()


def reconcile_mihomo_job(
    provider: MihomoProjectionProvider,
    loader: MihomoDesiredSnapshotLoader | None,
    resolver: ControllerSecretResolver | None,
    verifier: ProjectionVerifier | None,
    *,
    db_factory: Callable[[], Session] = SessionLocal,
) -> MihomoReconciliationResult | None:
    with db_factory() as db, mihomo_projection_write(db):
        if loader is None:
            settings = get_settings()
            loader = SqlMihomoDesiredSnapshotLoader(
                Path(settings.transport_cache_root), settings.mihomo_external_controller
            )
        if resolver is None:
            resolver = SqlMihomoControllerSecretResolver(db)
        if verifier is None:
            raise MihomoReconciliationError("MIHOMO_VERIFIER_NOT_CONFIGURED")
        job = _claim(db, datetime.now(UTC))
        if job is None:
            return None
        try:
            assert_no_unresolved_mihomo_mutation(db, owning_job_id=job.id)
        except MihomoReconciliationBlocked:
            _reset_blocked_claim(db, job)
            return None
        db.info[SESSION_INFO_RELEASE_METADATA_KEY] = {"source_job_id": job.id}
        try:
            try:
                payload = _payload(job)
                operation_id = _validate_operation_id(payload.get("operation_id"))
                operation_kind = _validate_operation_kind(payload.get("operation_kind"))
                expected = _validate_revision(payload.get("snapshot_revision"))
            except (MihomoPayloadError, ValueError):
                return _persist_unknown_blocker(
                    db,
                    job.id,
                    operation_id=None,
                    snapshot_revision=None,
                    diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
                    applied=False,
                )
            db.info[SESSION_INFO_RELEASE_METADATA_KEY] = {
                "source_job_id": job.id,
                "operation_id": operation_id,
                "snapshot_revision": expected,
            }
            desired = loader.load(db, job)
            if desired.snapshot_revision != expected:
                raise MihomoReconciliationError("MIHOMO_STALE_DESIRED_SNAPSHOT")
            template = provider.render(desired)
            candidate = provider.finalize(template, resolver)
            fingerprint = candidate.version
            try:
                apply_result = provider.apply(candidate)
            except MihomoRollbackUnknownError:
                return _mark_unknown(
                    db,
                    job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                    diagnostic=MIHOMO_ROLLBACK_UNKNOWN,
                )
            except (MihomoRollbackVerifiedError, MihomoApplyError) as exc:
                return _failure(db, job.id, exc, datetime.now(UTC))
            if getattr(apply_result, "applied", False) is not True:
                return _mark_unknown(
                    db,
                    job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                    diagnostic=MIHOMO_RUNTIME_UNKNOWN,
                    persist_blocker=True,
                )
            try:
                verification = verifier.verify(
                    candidate, operation_id=operation_id, snapshot_revision=expected
                )
            except Exception:
                return _mark_unknown(
                    db,
                    job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                    diagnostic=MIHOMO_RUNTIME_UNKNOWN,
                )
            if (
                not isinstance(verification, ProjectionVerification)
                or not verification.verified
                or not _valid_evidence_version(verification.evidence_version)
            ):
                return _mark_unknown(
                    db,
                    job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                    diagnostic=MIHOMO_RUNTIME_UNKNOWN,
                )
            payload["finalization_evidence"] = {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "snapshot_revision": expected,
                "candidate_fingerprint": fingerprint,
                "verification": verification.evidence_version,
            }
            job.payload_json = json.dumps(payload, sort_keys=True)
            job.status, job.locked_at, job.last_error_code = JobStatus.SUCCEEDED, None, None
            try:
                # Finalization success and release-pending are one durable boundary.
                ensure_mihomo_blocker(
                    db,
                    blocker_kind=MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
                    source_job_id=job.id,
                    operation_id=operation_id,
                    snapshot_revision=expected,
                    reason_code=MIHOMO_LOCK_RELEASE_PENDING,
                )
                db.commit()
            except Exception as commit_error:
                db.rollback()
                outcome = classify_finalization_outcome(
                    db,
                    job_id=job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                )
                if outcome is CommitOutcome.LANDED:
                    return MihomoReconciliationResult(job.id, True, True, False)
                if outcome is CommitOutcome.ABSENT:
                    return _failure(db, job.id, commit_error, datetime.now(UTC))
                return _mark_unknown(
                    db,
                    job.id,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    snapshot_revision=expected,
                    candidate_fingerprint=fingerprint,
                    diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
                )
            return MihomoReconciliationResult(job.id, True, True, False)
        except MihomoCommitOutcomeUnknown:
            raise
        except Exception as exc:
            return _failure(db, job.id, exc, datetime.now(UTC))
