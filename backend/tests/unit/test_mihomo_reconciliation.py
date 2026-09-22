from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import backend.app.core.secrets as secrets
import backend.app.models  # noqa: F401
from backend.app.core.database import Base
from backend.app.core.secrets import SecretSnapshot
from backend.app.infra import credential_resolver
from backend.app.infra import mihomo_reconciliation as reconciliation
from backend.app.infra.mihomo_blocker import (
    MIHOMO_BLOCKER_KIND_FINALIZATION,
    MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
    MIHOMO_DURABLE_STATE_UNKNOWN,
    MIHOMO_LOCK_RELEASE_PENDING,
    MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
    MihomoBlockerError,
    ensure_mihomo_blocker,
)
from backend.app.infra.mihomo_generation import (
    allocate_generation,
    build_projection_source_manifest,
    canonical_manifest,
    manifest_fingerprint,
)
from backend.app.infra.mihomo_projection_lock import (
    SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY,
    session_holds_mihomo_projection_lock,
)
from backend.app.infra.mihomo_reconciliation import (
    MIHOMO_FINALIZATION_UNKNOWN,
    MIHOMO_ROLLBACK_UNKNOWN,
    MIHOMO_RUNTIME_UNKNOWN,
    CommitOutcome,
    MihomoReconciliationBlocked,
    MihomoReconciliationError,
    MihomoReconciliationResult,
    ProjectionCandidate,
    ProjectionVerification,
    ProjectionVerifier,
    _failure,
    _mark_unknown,
    assert_no_unresolved_mihomo_mutation,
    classify_finalization_outcome,
    enqueue_mihomo_reconciliation,
    prepare_mihomo_reconciliation,
    reconcile_mihomo_job,
)
from backend.app.models import (
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    EgressTransportAssignment,
    Job,
    JobStatus,
    MihomoProjectionGeneration,
    MihomoTransportMaterialization,
    RouteBinding,
    RouteEgressBinding,
    RouteGroup,
    Secret,
    TrafficRule,
    TransportEndpointRecord,
    TransportProviderKind,
    TransportProviderRecord,
)
from backend.app.providers.base import (
    ApplyResult,
    DesiredForwarderState,
    EgressCredentialResolver,
    EgressCredentialSnapshot,
)
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretResolver,
    ControllerSecretSnapshot,
    MihomoFinalizationResolvers,
    MihomoForwarderProvider,
    MihomoRollbackUnknownError,
    MihomoRollbackVerifiedError,
    MihomoRuntime,
    MihomoRuntimeError,
)
from backend.app.providers.forwarder.mihomo_projection import (
    TransportMaterialization,
    TransportMaterializationReference,
)
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    cast(Table, Job.__table__).create(bind=engine)
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_finalization_fails_closed_on_egress_credential_revision_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine, db, loader = seed_mihomo_loader_graph(tmp_path)
    endpoint = db.get(EgressEndpoint, 1)
    assert endpoint is not None
    endpoint.protocol = "socks5"
    db.commit()
    desired = loader.load_current(db)
    generation = allocate_generation(db, build_projection_source_manifest(desired))
    db.commit()
    enqueue_mihomo_reconciliation(
        db,
        operation_id="revision-drift",
        operation_kind="RECONCILE",
        snapshot_revision=generation.revision,
    )

    def decrypt(ciphertext: str) -> str:
        return ciphertext

    monkeypatch.setattr(credential_resolver, "decrypt_secret", decrypt)
    monkeypatch.setattr(secrets, "decrypt_secret", lambda _ciphertext: "controller-secret")
    monkeypatch.setattr(
        reconciliation,
        "mihomo_projection_write",
        lambda session: _fake_lock_with_durable_fallback(session),
    )
    monkeypatch.setattr(
        reconciliation,
        "get_settings",
        lambda: SimpleNamespace(
            transport_cache_root=str(tmp_path), mihomo_external_controller="172.30.0.10:9090"
        ),
    )

    class Controller:
        def resolve(self, _ref: str) -> ControllerSecretSnapshot:
            return ControllerSecretSnapshot("mihomo/api-secret", 7, "controller")

    class Provider(MihomoForwarderProvider):
        def __init__(self) -> None:
            super().__init__(cast(MihomoRuntime, object()))
            self.finalization_error: MihomoRuntimeError | None = None
            self.apply_count = 0

        def render(self, desired: DesiredForwarderState) -> Any:
            template = super().render(
                replace(desired, transport_materializations=(), transport_references=())
            )
            secret = db.scalar(select(Secret).where(Secret.secret_ref == "egress/a"))
            assert secret is not None
            secret.revision = 4
            secret.ciphertext = json.dumps(
                {"username": "DRIFT_USER_SENTINEL", "password": "DRIFT_PASSWORD_SENTINEL"}
            )
            db.flush()
            return template

        def finalize(self, template: Any, resolvers: MihomoFinalizationResolvers) -> Any:
            try:
                return super().finalize(template, resolvers)
            except MihomoRuntimeError as exc:
                self.finalization_error = exc
                raise

        def apply(self, candidate: object) -> ApplyResult:
            self.apply_count += 1
            pytest.fail("apply must not run after credential revision drift")

    provider = Provider()
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        None,
        None,
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )
    assert result is not None
    assert result.applied is False
    assert provider.finalization_error is not None
    assert str(provider.finalization_error) == "MIHOMO_EGRESS_CREDENTIAL_REVISION_MISMATCH"
    assert provider.apply_count == 0
    leaked = " ".join(
        str(value)
        for row in db.scalars(select(Job)).all()
        for value in (row.payload_json, row.last_error_code)
    )
    assert "DRIFT_USER_SENTINEL" not in leaked
    assert "DRIFT_PASSWORD_SENTINEL" not in leaked
    assert "DRIFT_USER_SENTINEL" not in " ".join(record.getMessage() for record in caplog.records)
    assert "DRIFT_PASSWORD_SENTINEL" not in " ".join(
        record.getMessage() for record in caplog.records
    )
    blockers = db.scalars(
        select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
    ).all()
    for blocker in blockers:
        assert "DRIFT_USER_SENTINEL" not in str(blocker.payload_json)
        assert "DRIFT_PASSWORD_SENTINEL" not in str(blocker.payload_json)
        assert "DRIFT_USER_SENTINEL" not in str(blocker.last_error_code)
        assert "DRIFT_PASSWORD_SENTINEL" not in str(blocker.last_error_code)
    engine.dispose()


def test_egress_plaintext_is_only_decrypted_inside_the_projection_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, db, loader = seed_mihomo_loader_graph(tmp_path)
    endpoint = db.get(EgressEndpoint, 1)
    assert endpoint is not None
    endpoint.protocol = "socks5"
    db.commit()
    desired = loader.load_current(db)
    generation = allocate_generation(db, build_projection_source_manifest(desired))
    db.commit()
    enqueue_mihomo_reconciliation(
        db,
        operation_id="lock-boundary",
        operation_kind="RECONCILE",
        snapshot_revision=generation.revision,
    )
    seen: list[bool] = []

    def decrypt(_ciphertext: str) -> str:
        seen.append(session_holds_mihomo_projection_lock(db))
        assert seen[-1] is True
        return json.dumps({"username": "user", "password": "pass"})

    monkeypatch.setattr(credential_resolver, "decrypt_secret", decrypt)
    monkeypatch.setattr(secrets, "decrypt_secret", lambda _ciphertext: "controller-secret")
    monkeypatch.setattr(
        reconciliation,
        "mihomo_projection_write",
        lambda session: _fake_lock_with_durable_fallback(session),
    )
    monkeypatch.setattr(
        reconciliation,
        "get_settings",
        lambda: SimpleNamespace(
            transport_cache_root=str(tmp_path), mihomo_external_controller="172.30.0.10:9090"
        ),
    )

    class Provider(MihomoForwarderProvider):
        def __init__(self) -> None:
            super().__init__(cast(MihomoRuntime, object()))
            self.captured: object | None = None

        def render(self, desired: DesiredForwarderState) -> Any:
            return super().render(
                replace(desired, transport_materializations=(), transport_references=())
            )

        def apply(self, candidate: object) -> ApplyResult:
            self.captured = candidate
            return ApplyResult(applied=True, version="captured")

    provider = Provider()
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        None,
        None,
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )
    assert result is not None
    assert result.applied is True
    assert provider.captured is not None
    assert seen and all(seen)
    db.close()
    engine.dispose()


def test_reconcile_resolves_egress_plaintext_with_no_injected_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, db, loader = seed_mihomo_loader_graph(tmp_path)
    endpoint = db.get(EgressEndpoint, 1)
    assert endpoint is not None
    endpoint.protocol = "socks5"
    db.commit()
    desired = loader.load_current(db)
    generation = allocate_generation(db, build_projection_source_manifest(desired))
    db.commit()
    enqueue_mihomo_reconciliation(
        db,
        operation_id="default-resolvers",
        operation_kind="RECONCILE",
        snapshot_revision=generation.revision,
    )
    egress_secret = db.scalar(
        select(Secret).where(Secret.secret_ref == "egress/a")
    )
    assert egress_secret is not None
    egress_secret.ciphertext = json.dumps(
        {"username": "REAL_EGRESS_USER_SENTINEL", "password": "REAL_EGRESS_PASSWORD_SENTINEL"}
    )
    controller_secret = db.scalar(
        select(Secret).where(Secret.secret_ref == credential_resolver.MIHOMO_CONTROLLER_SECRET_REF)
    )
    assert controller_secret is not None
    controller_secret.ciphertext = "controller-secret"
    db.commit()
    monkeypatch.setattr(
        reconciliation,
        "mihomo_projection_write",
        lambda session: _fake_lock_with_durable_fallback(session),
    )
    monkeypatch.setattr(
        reconciliation,
        "get_settings",
        lambda: SimpleNamespace(
            transport_cache_root=str(tmp_path), mihomo_external_controller="172.30.0.10:9090"
        ),
    )

    def decrypt(ciphertext: str) -> str:
        return ciphertext

    monkeypatch.setattr(credential_resolver, "decrypt_secret", decrypt)
    monkeypatch.setattr(secrets, "decrypt_secret", decrypt)
    original_load = reconciliation.SqlMihomoDesiredSnapshotLoader.load

    def sqlite_aware_load(
        loader_instance: reconciliation.SqlMihomoDesiredSnapshotLoader,
        session: Session,
        job: Job,
    ) -> DesiredForwarderState:
        loaded = original_load(loader_instance, session, job)
        return replace(
            loaded,
            transport_materializations=(),
            transport_references=(),
        )

    monkeypatch.setattr(reconciliation.SqlMihomoDesiredSnapshotLoader, "load", sqlite_aware_load)

    class Provider(MihomoForwarderProvider):
        def __init__(self) -> None:
            super().__init__(cast(MihomoRuntime, object()))
            self.captured: object | None = None

        def apply(self, candidate: object) -> ApplyResult:
            self.captured = candidate
            return ApplyResult(applied=True, version="captured")

    provider = Provider()
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        None,
        None,
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "v1"))),
        db_factory=lambda: db,
    )
    assert result is not None
    assert provider.captured is not None
    captured = cast(Any, provider.captured)
    egress = next(item for item in captured.content["proxies"] if item["name"] == "egress-a")
    assert egress["username"] == "REAL_EGRESS_USER_SENTINEL"
    assert egress["password"] == "REAL_EGRESS_PASSWORD_SENTINEL"
    db.close()
    engine.dispose()


def test_prepare_generation_and_intent_commit_together(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    cast(Table, MihomoProjectionGeneration.__table__).create(bind=db.get_bind())
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", nullcontext)

    class Loader:
        def load_current(self, session: Session) -> DesiredForwarderState:
            assert session is db
            return DesiredForwarderState(snapshot_revision=1, snapshot_identity="input")

    job = prepare_mihomo_reconciliation(db, operation_id="prepare-together", loader=Loader())
    generation = db.scalar(select(MihomoProjectionGeneration))
    persisted_job = db.get(Job, job.id)
    assert generation is not None
    assert persisted_job is not None
    payload = json.loads(persisted_job.payload_json)
    assert payload["snapshot_revision"] == generation.revision
    assert persisted_job.status is JobStatus.PENDING


def test_prepare_rolls_back_generation_when_intent_enqueue_fails(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    cast(Table, MihomoProjectionGeneration.__table__).create(bind=db.get_bind())
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", nullcontext)

    class Loader:
        def load_current(self, _session: Session) -> DesiredForwarderState:
            return DesiredForwarderState()

    def fail_enqueue(*_args: object, **_kwargs: object) -> Job:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(reconciliation, "enqueue_mihomo_reconciliation", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue failed"):
        prepare_mihomo_reconciliation(db, operation_id="rollback", loader=Loader())
    db.commit()
    assert db.scalar(select(MihomoProjectionGeneration.revision)) is None
    assert db.scalar(select(Job.id)) is None


def test_sql_controller_secret_resolver_requires_exact_purpose_and_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def reveal(_db: Session, ref: str, purpose: str) -> SecretSnapshot:
        seen.append((ref, purpose))
        return SecretSnapshot("controller-secret", 4)

    monkeypatch.setattr(credential_resolver, "reveal_secret_snapshot_for_purpose", reveal)
    resolver = credential_resolver.SqlMihomoControllerSecretResolver(cast(Session, object()))
    snapshot = resolver.resolve(credential_resolver.MIHOMO_CONTROLLER_SECRET_REF)
    assert snapshot.revision == 4
    assert seen == [
        (
            "mihomo/api-secret",
            credential_resolver.MIHOMO_CONTROLLER_SECRET_PURPOSE,
        )
    ]


def seed_mihomo_loader_graph(
    tmp_path: Path,
    *,
    endpoints: list[dict[str, Any]] | None = None,
    insertion_order: list[str] | None = None,
    binding_credential_secret_ref: str | None = None,
    binding_egress_id: int = 1,
    assignments: list[dict[str, Any]] | None = None,
    assignment_node_id: int = 11,
    transport_nodes: list[dict[str, Any]] | None = None,
    transport_non_identity: dict[str, object] | None = None,
    secret_revisions: dict[str, int] | None = None,
    transport_cache_content: bytes | None = None,
    source_revision: int = 2,
) -> tuple[Engine, Session, reconciliation.SqlMihomoDesiredSnapshotLoader]:
    """Seed the complete minimal loader graph; later checkpoints vary its axes."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(
        engine,
        "connect",
        lambda connection, _record: connection.create_function(
            "IF",
            3,
            lambda condition, when_true, when_false: when_true if condition else when_false,
            deterministic=True,
        ),
    )
    tables = [
        Job.__table__,
        RouteGroup.__table__,
        TransportProviderRecord.__table__,
        RouteBinding.__table__,
        EgressGroup.__table__,
        EgressEndpoint.__table__,
        RouteEgressBinding.__table__,
        TrafficRule.__table__,
        MihomoTransportMaterialization.__table__,
        MihomoProjectionGeneration.__table__,
        TransportEndpointRecord.__table__,
        EgressTransportAssignment.__table__,
        EgressBinding.__table__,
        Secret.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)  # type: ignore[arg-type]
    content = (
        transport_cache_content
        or b"proxies:\n  - {name: Node A, type: ss, server: 203.0.113.11, port: 443}\n"
    )
    resolver = SubscriptionTransportResolver(tmp_path)
    descriptor = TransportProviderDescriptor(
        1, "transport-a", "SUBSCRIPTION", "transport/subscription-a"
    )
    cache_path = resolver._cache_path(descriptor)  # noqa: SLF001
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    now = datetime.now(UTC)
    endpoint_rows = endpoints or [{"id": 1, "code": "egress-a", "status": "AVAILABLE"}]
    rows_by_code = {str(row.get("code", "egress-a")): row for row in endpoint_rows}
    order = insertion_order or list(reversed(list(rows_by_code)))
    revisions = secret_revisions or {"egress/a": 3}
    non_identity = transport_non_identity or {}
    node_rows = transport_nodes or [
        {
            "id": 11,
            "external_id": "node-a",
            "name": "Node A",
            "host": "203.0.113.11",
            "port": 443,
        }
    ]
    with Session(engine) as session:
        from backend.app.models import ProviderStatus, RouteGroupStatus

        session.add_all(
            [
                TransportProviderRecord(
                    id=1,
                    code="transport-a",
                    slug="transport-a",
                    name="Transport A",
                    kind=TransportProviderKind.SUBSCRIPTION,
                    secret_ref="transport/subscription-a",
                    status=ProviderStatus.HEALTHY,
                    enabled=True,
                ),
                Secret(
                    secret_ref="transport/subscription-a",
                    ciphertext="opaque",
                    purpose="TRANSPORT_SUBSCRIPTION_URL",
                    revision=source_revision,
                ),
                Secret(
                    secret_ref=credential_resolver.MIHOMO_CONTROLLER_SECRET_REF,
                    ciphertext="opaque-controller",
                    purpose=credential_resolver.MIHOMO_CONTROLLER_SECRET_PURPOSE,
                    revision=7,
                ),
                RouteGroup(id=1, code="route-a", name="A", status=RouteGroupStatus.ACTIVE),
                EgressGroup(id=1, code="egress", region="test", status="ACTIVE"),
                RouteBinding(route_group_id=1, provider_id=1, role="PRIMARY", enabled=True),
                TrafficRule(
                    rule_set="default",
                    match_type="MATCH",
                    target_egress="BLOCK",
                    priority=100,
                    enabled=True,
                ),
                MihomoTransportMaterialization(
                    owner_record_id=1,
                    provider_code="transport-a",
                    source_revision=source_revision,
                    cache_identity=str(cache_path),
                    content_hash=hashlib.sha256(content).hexdigest(),
                    freshness_deadline=now + timedelta(hours=1),
                ),
            ]
        )
        for node in node_rows:
            session.add(
                TransportEndpointRecord(
                    id=int(node.get("id", 11)),
                    provider_id=int(node.get("provider_id", 1)),
                    external_id=str(node.get("external_id", "node-a")),
                    name=str(node.get("name", "Node A")),
                    protocol=str(node.get("protocol", "https")),
                    host=str(node.get("host", "203.0.113.11")),
                    port=int(node.get("port", 443)),
                    auth_secret_ref=str(node.get("auth_secret_ref", "node/a")),
                    status=non_identity.get("status", node.get("status", "AVAILABLE")),
                    region=non_identity.get("region", node.get("region", "UNKNOWN")),
                    latency_ms=non_identity.get("latency_ms", node.get("latency_ms")),
                    packet_loss=None,
                    raw_metadata_json=non_identity.get(
                        "raw_metadata_json", node.get("raw_metadata_json", "{}")
                    ),
                )
            )
        for index, code in enumerate(order, 1):
            row = rows_by_code[code]
            endpoint_id = int(row.get("id", index))
            ref = str(row.get("credential_secret_ref", f"egress/{code[-1]}"))
            session.add(
                EgressEndpoint(
                    id=endpoint_id,
                    group_id=1,
                    code=code,
                    provider_name="test",
                    host=str(row.get("host", "203.0.113.10")),
                    port=int(row.get("port", 443)),
                    protocol="ss",
                    credential_secret_ref=ref,
                    mihomo_listen_port=10000 + endpoint_id,
                    status=str(row.get("status", "AVAILABLE")),
                )
            )
            session.add(
                Secret(
                    secret_ref=ref,
                    ciphertext="opaque-egress",
                    purpose="EGRESS_CREDENTIAL",
                    revision=revisions.get(ref, 3),
                )
            )
            session.add(
                RouteEgressBinding(
                    route_group_id=1, egress_endpoint_id=endpoint_id, role="PRIMARY", enabled=True
                )
            )
        session.add(
            EgressBinding(
                subscription_id=10,
                egress_id=binding_egress_id,
                credential_secret_ref=binding_credential_secret_ref,
            )
        )
        for item in assignments or [
            {
                "egress_id": 1,
                "transport_provider_id": 1,
                "transport_node_id": assignment_node_id,
                "state": "ACTIVE",
            }
        ]:
            session.add(
                EgressTransportAssignment(
                    egress_id=int(item.get("egress_id", 1)),
                    transport_provider_id=int(item.get("transport_provider_id", 1)),
                    transport_node_id=int(item.get("transport_node_id", assignment_node_id)),
                    state=str(item.get("state", "ACTIVE")),
                    allocation_generation=1,
                    last_latency_ms=None,
                    last_checked_at=None,
                    unavailable_since=None,
                )
            )
        session.commit()
        loader = reconciliation.SqlMihomoDesiredSnapshotLoader(tmp_path, "203.0.113.20:9090")
        return engine, session, loader


def test_sql_desired_loader_rebuilds_db_authority_with_stable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        endpoints=[
            {"id": 1, "code": "egress-b", "status": "AVAILABLE"},
            {"id": 2, "code": "egress-a", "status": "AVAILABLE"},
        ],
        insertion_order=["egress-b", "egress-a"],
    )
    monkeypatch.setattr(
        credential_resolver,
        "reveal_secret_snapshot_for_purpose",
        lambda *_args: pytest.fail("loader must not resolve controller plaintext"),
    )
    first = loader.load_current(session)
    second = loader.load_current(session)
    assert tuple(item.name for item in first.egress_proxies) == ("egress-a", "egress-b")
    assert tuple(item["name"] for item in first.proxy_groups) == ("route-a",)
    assert first == second
    assert first.deployment_constants["api-secret-revision"] == 7
    session.close()
    engine.dispose()
def test_whitespace_only_credential_override_fails_closed(
    tmp_path: Path,
) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path, binding_credential_secret_ref="   "
    )
    with pytest.raises(
        MihomoReconciliationError, match="MIHOMO_EGRESS_CREDENTIAL_REF_INVALID"
    ):
        loader.load_current(session)
    session.close()
    engine.dispose()


def test_loader_populates_egress_proxies_with_opaque_credential_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_plaintext(*_args: object, **_kwargs: object) -> str:
        pytest.fail("loader must not resolve credential plaintext")

    monkeypatch.setattr(secrets, "reveal_secret_for_purpose", fail_plaintext)
    monkeypatch.setattr(secrets, "reveal_secret", fail_plaintext)
    monkeypatch.setattr(secrets, "decrypt_secret", fail_plaintext)
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        endpoints=[
            {"id": 1, "code": "egress-b", "status": "AVAILABLE"},
            {"id": 2, "code": "egress-a", "status": "AVAILABLE"},
        ],
        insertion_order=["egress-b", "egress-a"],
    )
    binding = session.scalar(select(EgressBinding))
    assert binding is not None
    binding.credential_secret_ref = "override/ref"
    session.add(
        Secret(
            secret_ref="override/ref",
            ciphertext="opaque-override",
            purpose="EGRESS_CREDENTIAL",
            revision=9,
        )
    )
    session.commit()
    desired = loader.load_current(session)
    assert tuple(item.name for item in desired.egress_proxies) == ("egress-a", "egress-b")
    first, second = desired.egress_proxies
    assert first.credential_secret_ref == "egress/a"
    assert first.credential_revision == 3
    assert second.credential_secret_ref == "override/ref"
    assert second.credential_revision == 9
    assert second.transport_proxy_name == "Node A"
    assert second.transport_node_identity == ("Node A", "203.0.113.11", 443)
    session.close()
    engine.dispose()


def test_active_binding_credential_override_is_preserved_as_opaque_identity(
    tmp_path: Path,
) -> None:
    cases = [
        ("override/ref", "override/ref", None),
        (None, "egress/a", None),
        ("", "egress/a", None),
        ("missing/ref", None, "MIHOMO_EGRESS_CREDENTIAL_METADATA_INVALID"),
        ("invalid/revision", None, "MIHOMO_EGRESS_CREDENTIAL_METADATA_INVALID"),
    ]
    for index, (override, expected_ref, expected_error) in enumerate(cases):
        engine, session, loader = seed_mihomo_loader_graph(tmp_path / str(index))
        binding = session.scalar(select(EgressBinding))
        assert binding is not None
        binding.credential_secret_ref = override
        if override == "override/ref":
            session.add(
                Secret(
                    secret_ref=override,
                    ciphertext="opaque-override",
                    purpose="EGRESS_CREDENTIAL",
                    revision=9,
                )
            )
        elif override == "invalid/revision":
            session.add(
                Secret(
                    secret_ref=override,
                    ciphertext="opaque-invalid",
                    purpose="EGRESS_CREDENTIAL",
                    revision=0,
                )
            )
        session.commit()
        if expected_error is None:
            desired = loader.load_current(session)
            assert desired.egress_proxies[0].credential_secret_ref == expected_ref
        else:
            with pytest.raises(MihomoReconciliationError, match=expected_error):
                loader.load_current(session)
        session.close()
        engine.dispose()


def test_assignment_node_change_changes_manifest_fingerprint(tmp_path: Path) -> None:
    cache = (
        b"proxies:\n"
        b"  - {name: Node A, type: ss, server: 203.0.113.11, port: 443}\n"
        b"  - {name: Node B, type: ss, server: 203.0.113.12, port: 444}\n"
    )
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        transport_nodes=[
            {
                "id": 11,
                "external_id": "node-a",
                "name": "Node A",
                "host": "203.0.113.11",
                "port": 443,
            },
            {
                "id": 12,
                "external_id": "node-b",
                "name": "Node B",
                "host": "203.0.113.12",
                "port": 444,
            },
        ],
        transport_cache_content=cache,
        assignment_node_id=11,
    )
    assignment = session.scalar(select(EgressTransportAssignment))
    assert assignment is not None

    desired_a = loader.load_current(session)
    manifest_a = build_projection_source_manifest(desired_a)
    generation_a = allocate_generation(session, manifest_a)
    session.commit()

    assignment.transport_node_id = 12
    session.commit()
    desired_b = loader.load_current(session)
    manifest_b = build_projection_source_manifest(desired_b)
    generation_b = allocate_generation(session, manifest_b)
    session.commit()

    assert desired_a.egress_proxies[0].transport_proxy_name == "Node A"
    assert desired_b.egress_proxies[0].transport_proxy_name == "Node B"
    assert desired_a.egress_proxies[0].transport_node_identity == (
        "Node A",
        "203.0.113.11",
        443,
    )
    assert desired_b.egress_proxies[0].transport_node_identity == (
        "Node B",
        "203.0.113.12",
        444,
    )
    assert manifest_fingerprint(manifest_a) != manifest_fingerprint(manifest_b)
    assert generation_b.revision > generation_a.revision
    session.close()
    engine.dispose()


def test_binding_credential_ref_change_changes_manifest_fingerprint(tmp_path: Path) -> None:
    engine, session, loader = seed_mihomo_loader_graph(tmp_path / "ref-change")
    binding = session.scalar(select(EgressBinding))
    assert binding is not None
    session.add(
        Secret(
            secret_ref="egress/b",
            ciphertext="opaque-b",
            purpose="EGRESS_CREDENTIAL",
            revision=4,
        )
    )
    binding.credential_secret_ref = "egress/b"
    session.commit()

    desired_a = loader.load_current(session)
    manifest_a = build_projection_source_manifest(desired_a)
    generation_a = allocate_generation(session, manifest_a)
    session.commit()
    binding.credential_secret_ref = "egress/a"
    session.commit()
    desired_b = loader.load_current(session)
    manifest_b = build_projection_source_manifest(desired_b)
    generation_b = allocate_generation(session, manifest_b)
    session.commit()
    assert manifest_fingerprint(manifest_a) != manifest_fingerprint(manifest_b)
    assert generation_b.revision > generation_a.revision
    session.close()
    engine.dispose()

    engine, session, loader = seed_mihomo_loader_graph(tmp_path / "revision-change")
    desired_n = loader.load_current(session)
    manifest_n = build_projection_source_manifest(desired_n)
    generation_n = allocate_generation(session, manifest_n)
    session.commit()
    secret = session.scalar(select(Secret).where(Secret.secret_ref == "egress/a"))
    assert secret is not None
    secret.revision = 4
    session.commit()
    desired_n1 = loader.load_current(session)
    manifest_n1 = build_projection_source_manifest(desired_n1)
    generation_n1 = allocate_generation(session, manifest_n1)
    session.commit()
    assert manifest_fingerprint(manifest_n) != manifest_fingerprint(manifest_n1)
    assert generation_n1.revision > generation_n.revision
    session.close()
    engine.dispose()


def test_transport_endpoint_record_non_identity_columns_are_not_inputs(
    tmp_path: Path,
) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        transport_non_identity={
            "region": "region-a",
            "latency_ms": 10,
            "status": "HEALTHY",
            "raw_metadata_json": '{"zone":"a"}',
        },
    )
    node = session.scalar(select(TransportEndpointRecord))
    assert node is not None
    identity = (node.name, node.host, node.port)
    desired_a = loader.load_current(session)
    fingerprint_a = manifest_fingerprint(build_projection_source_manifest(desired_a))

    node.region = "region-b"
    node.latency_ms = 99
    node.status = "DEGRADED"
    node.raw_metadata_json = '{"zone":"b","changed":true}'
    session.commit()
    desired_b = loader.load_current(session)
    fingerprint_b = manifest_fingerprint(build_projection_source_manifest(desired_b))

    assert (node.name, node.host, node.port) == identity
    assert fingerprint_b == fingerprint_a
    session.close()
    engine.dispose()


def test_assigned_and_degraded_endpoints_are_in_scope(tmp_path: Path) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        endpoints=[
            {"id": 1, "code": "egress-a", "status": "AVAILABLE"},
            {"id": 2, "code": "egress-b", "status": "ASSIGNED"},
            {"id": 3, "code": "egress-c", "status": "DEGRADED"},
        ],
        insertion_order=["egress-c", "egress-a", "egress-b"],
    )
    desired = loader.load_current(session)
    assert len(desired.listener_specs) == 3
    assert tuple(listener.proxy for listener in desired.listener_specs) == (
        "egress-a",
        "egress-b",
        "egress-c",
    )
    session.close()
    engine.dispose()


def test_egress_binding_egress_id_mismatch_fails_closed(tmp_path: Path) -> None:
    engine, session, loader = seed_mihomo_loader_graph(tmp_path, binding_egress_id=999)
    with pytest.raises(MihomoReconciliationError, match="MIHOMO_EGRESS_BINDING_INVALID"):
        loader.load_current(session)
    session.close()
    engine.dispose()


def test_synthetic_subscription_proxy_group_is_not_used(tmp_path: Path) -> None:
    sentinel_subscription_id = 987654321
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        endpoints=[
            {"id": 1, "code": "egress-a", "status": "AVAILABLE"},
            {"id": 2, "code": "egress-b", "status": "DEGRADED"},
        ],
        insertion_order=["egress-b", "egress-a"],
    )
    binding = session.scalar(select(EgressBinding))
    assert binding is not None
    binding.subscription_id = sentinel_subscription_id
    session.commit()
    desired = loader.load_current(session)
    assert not any(
        str(group["name"]).startswith("subscription-") for group in desired.proxy_groups
    )

    def assert_without_subscription_id(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                assert str(sentinel_subscription_id) not in str(key)
                assert str(sentinel_subscription_id) not in str(item)
                assert_without_subscription_id(key)
                assert_without_subscription_id(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert str(sentinel_subscription_id) not in str(item)
                assert_without_subscription_id(item)
        elif is_dataclass(value):
            for field in fields(value):
                item = getattr(value, field.name)
                assert str(sentinel_subscription_id) not in field.name
                assert str(sentinel_subscription_id) not in str(item)
                assert_without_subscription_id(item)
        else:
            assert str(sentinel_subscription_id) not in str(value)

    for structure in (
        desired.proxy_groups,
        desired.proxies,
        desired.listener_specs,
        build_projection_source_manifest(desired),
    ):
        assert_without_subscription_id(structure)
    session.close()
    engine.dispose()


def test_loader_rejects_assignment_with_missing_transport_endpoint(tmp_path: Path) -> None:
    engine, session, loader = seed_mihomo_loader_graph(tmp_path, assignment_node_id=999)
    with pytest.raises(
        MihomoReconciliationError, match="MIHOMO_TRANSPORT_ASSIGNMENT_NODE_MISSING"
    ):
        loader.load_current(session)
    session.close()
    engine.dispose()


def test_loader_rejects_assignment_provider_mismatch(tmp_path: Path) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        transport_nodes=[
            {
                "id": 11,
                "provider_id": 2,
                "external_id": "node-a",
                "name": "Node A",
                "host": "203.0.113.11",
                "port": 443,
            }
        ],
    )
    with pytest.raises(
        MihomoReconciliationError, match="MIHOMO_TRANSPORT_ASSIGNMENT_PROVIDER_MISMATCH"
    ):
        loader.load_current(session)
    session.close()
    engine.dispose()


@pytest.mark.parametrize("state", ["RELEASED", "DRAINING"])
def test_released_transport_assignment_does_not_block_projection(
    tmp_path: Path, state: str
) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path,
        assignments=[
            {
                "egress_id": 1,
                "transport_provider_id": 999,
                "transport_node_id": 999,
                "state": state,
            }
        ],
    )
    with_historical_row = loader.load_current(session)
    assert with_historical_row.egress_proxies[0].transport_proxy_name is None
    assert with_historical_row.egress_proxies[0].transport_node_identity is None
    assignment = session.scalar(select(EgressTransportAssignment))
    assert assignment is not None
    session.delete(assignment)
    session.commit()
    without_assignment_row = loader.load_current(session)
    assert with_historical_row == without_assignment_row
    with_row_bytes = canonical_manifest(
        build_projection_source_manifest(with_historical_row)
    ).encode()
    without_row_bytes = canonical_manifest(
        build_projection_source_manifest(without_assignment_row)
    ).encode()
    assert with_row_bytes == without_row_bytes
    session.close()
    engine.dispose()


@pytest.mark.parametrize(
    "content",
    [
        b"proxies:\n  - {name: wrong, type: ss, server: 203.0.113.11, port: 443}\n",
        b"proxies:\n  - {name: Node A, type: ss, server: 203.0.113.11, port: 443}\n"
        b"  - {name: Node A, type: ss, server: 203.0.113.11, port: 443}\n",
    ],
)
def test_transport_proxy_unresolved_fails_closed(
    tmp_path: Path, content: bytes
) -> None:
    engine, session, loader = seed_mihomo_loader_graph(
        tmp_path, transport_cache_content=content
    )
    with pytest.raises(
        MihomoReconciliationError, match="MIHOMO_TRANSPORT_PROXY_UNRESOLVED"
    ):
        loader.load_current(session)
    session.close()
    engine.dispose()


def test_sql_desired_loader_binds_exact_job_generation_identity(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=cast(list[Table], [Job.__table__, MihomoProjectionGeneration.__table__]),
    )
    loader = reconciliation.SqlMihomoDesiredSnapshotLoader(tmp_path, "127.0.0.1:9090")
    with Session(engine) as session:
        generation = MihomoProjectionGeneration(
            revision=7, manifest_version="1", desired_fingerprint="a" * 64
        )
        session.add(generation)
        session.flush()
        job = Job(
            job_type="MIHOMO_RECONCILE",
            dedupe_key="exact-generation",
            payload_json=json.dumps({"snapshot_revision": 7}),
        )
        session.add(job)
        session.flush()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(loader, "load_current", lambda _db: DesiredForwarderState())
        desired = loader.load(session, job)
        assert desired.snapshot_revision == 7
        assert desired.snapshot_identity == f"mihomo:1:7:{'a' * 64}"
        monkeypatch.undo()
        job.payload_json = json.dumps({"snapshot_revision": 8})
        with pytest.raises(MihomoReconciliationError, match="GENERATION_MISSING"):
            loader.load(session, job)


def test_enqueue_identity_is_transactional_and_exact(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    db.rollback()
    assert db.get(Job, job.id) is None

    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    db.commit()
    same = enqueue_mihomo_reconciliation(
        db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=2
    )
    assert same.id == job.id
    with pytest.raises(MihomoReconciliationError, match="IDENTITY_MISMATCH"):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="UPDATE", snapshot_revision=2
        )
    with pytest.raises(MihomoReconciliationError, match="IDENTITY_MISMATCH"):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="RECONCILE", snapshot_revision=3
        )


def test_enqueue_rejects_noncanonical_operation_identity(db: Session) -> None:
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db, operation_id=" op-1", operation_kind="RECONCILE", snapshot_revision=1
        )


def test_operation_id_is_bounded_by_job_dedupe_key(db: Session) -> None:
    operation_id = "x" * 143
    job = enqueue_mihomo_reconciliation(
        db, operation_id=operation_id, operation_kind="RECONCILE", snapshot_revision=1
    )
    assert len(job.dedupe_key) == 160
    db.rollback()
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db,
            operation_id=operation_id + "x",
            operation_kind="RECONCILE",
            snapshot_revision=1,
        )


@pytest.mark.parametrize(
    "operation_id",
    ["abc", "op-123", "op_123", "op.123", "op:123", "x" * 143],
)
def test_operation_id_accepts_only_shared_safe_grammar(db: Session, operation_id: str) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id=operation_id, operation_kind="RECONCILE", snapshot_revision=1
    )
    assert job.dedupe_key == f"MIHOMO_RECONCILE:{operation_id}"


@pytest.mark.parametrize(
    "operation_id",
    [" op", "op ", "op/123", r"op\123", "op 123", "中文", "op\x01", "x" * 144],
)
def test_operation_id_rejects_values_not_persistable_in_blockers(
    db: Session, operation_id: str
) -> None:
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db, operation_id=operation_id, operation_kind="RECONCILE", snapshot_revision=1
        )


def test_blocker_is_identifier_only_idempotent_and_global(db: Session) -> None:
    blocker = ensure_mihomo_blocker(
        db,
        blocker_kind=MIHOMO_BLOCKER_KIND_FINALIZATION,
        source_job_id=77,
        operation_id="op-blocked",
        snapshot_revision=1,
        reason_code=MIHOMO_FINALIZATION_UNKNOWN,
    )
    db.commit()
    same = ensure_mihomo_blocker(
        db,
        blocker_kind=MIHOMO_BLOCKER_KIND_FINALIZATION,
        source_job_id=77,
        operation_id="op-blocked",
        snapshot_revision=1,
        reason_code=MIHOMO_FINALIZATION_UNKNOWN,
    )
    assert same.id == blocker.id
    assert "secret" not in same.payload_json.casefold()
    assert "token" not in same.payload_json.casefold()
    assert (
        db.scalar(select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)) is not None
    )
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked, match="GLOBAL_MANUAL_BLOCKER"):
        assert_no_unresolved_mihomo_mutation(db)
    with pytest.raises(MihomoBlockerError):
        ensure_mihomo_blocker(
            db,
            blocker_kind=MIHOMO_BLOCKER_KIND_FINALIZATION,
            source_job_id=78,
            operation_id="op/123",
            snapshot_revision=1,
            reason_code=MIHOMO_FINALIZATION_UNKNOWN,
        )
    with pytest.raises(ValueError):
        enqueue_mihomo_reconciliation(
            db, operation_id="op-1", operation_kind="ARBITRARY", snapshot_revision=1
        )


def test_guard_requires_lock_and_blocks_older_unresolved_intent(db: Session) -> None:
    older = enqueue_mihomo_reconciliation(
        db, operation_id="older", operation_kind="RECONCILE", snapshot_revision=1
    )
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    with pytest.raises(MihomoReconciliationBlocked, match="LOCK_REQUIRED"):
        assert_no_unresolved_mihomo_mutation(db)
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked, match="UNRESOLVED_INTENT"):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)
    older.status = JobStatus.FAILED
    older.attempts = older.max_attempts
    db.commit()
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


def test_claims_pending_retryable_and_stale_running_but_not_unknown(
    db: Session,
) -> None:
    from backend.app.infra.mihomo_reconciliation import _claim

    pending = enqueue_mihomo_reconciliation(
        db, operation_id="claim-pending", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    claimed = _claim(db, now)
    assert claimed is not None and claimed.id == pending.id
    claimed.status = JobStatus.FAILED
    claimed.locked_at = None
    claimed.available_at = now - timedelta(seconds=1)
    db.commit()
    retry = _claim(db, now)
    assert retry is not None and retry.id == pending.id
    retry.status = JobStatus.SUCCEEDED
    retry.locked_at = None
    db.commit()

    stale = enqueue_mihomo_reconciliation(
        db, operation_id="claim-stale", operation_kind="RECONCILE", snapshot_revision=1
    )
    stale.status = JobStatus.RUNNING
    stale.locked_at = now - timedelta(minutes=20)
    stale.available_at = now - timedelta(seconds=1)
    db.commit()
    claimed_stale = _claim(db, now)
    assert claimed_stale is not None and claimed_stale.id == stale.id
    claimed_stale.status = JobStatus.SUCCEEDED
    claimed_stale.locked_at = None
    db.commit()

    unknown = enqueue_mihomo_reconciliation(
        db, operation_id="claim-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    unknown.status = JobStatus.RUNNING
    unknown.locked_at = now - timedelta(minutes=20)
    unknown.last_error_code = "MIHOMO_FINALIZATION_COMMIT_OUTCOME_UNKNOWN"
    unknown.available_at = now - timedelta(seconds=1)
    db.commit()
    assert _claim(db, now) is None


def test_classifier_requires_exact_job_and_evidence(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-2", operation_kind="RECONCILE", snapshot_revision=4
    )
    db.commit()
    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.UNKNOWN
    )
    job.status = JobStatus.RUNNING
    db.commit()
    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.ABSENT
    )
    assert (
        classify_finalization_outcome(
            db,
            job_id=9999,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.UNKNOWN
    )
    payload = json.loads(job.payload_json)
    payload["finalization_evidence"] = {
        "operation_id": "op-2",
        "operation_kind": "RECONCILE",
        "snapshot_revision": 4,
        "candidate_fingerprint": "v4",
        "verification": "readback-v1",
    }
    job.payload_json = json.dumps(payload)
    job.status = JobStatus.SUCCEEDED
    db.commit()
    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.UNKNOWN
    )
    ensure_mihomo_blocker(
        db,
        blocker_kind=MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
        source_job_id=job.id,
        operation_id="op-2",
        snapshot_revision=4,
        reason_code=MIHOMO_LOCK_RELEASE_PENDING,
    )
    db.commit()
    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.LANDED
    )
    payload["finalization_evidence"]["candidate_fingerprint"] = "wrong"
    job.payload_json = json.dumps(payload)
    db.commit()
    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-2",
            operation_kind="RECONCILE",
            snapshot_revision=4,
            candidate_fingerprint="v4",
        )
        is CommitOutcome.UNKNOWN
    )


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.FAILED])
def test_classifier_never_calls_pending_or_failed_absent(db: Session, status: JobStatus) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-certainty", operation_kind="RECONCILE", snapshot_revision=1
    )
    job.status = status
    db.commit()

    assert (
        classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="op-certainty",
            operation_kind="RECONCILE",
            snapshot_revision=1,
            candidate_fingerprint="projection-v1",
        )
        is CommitOutcome.UNKNOWN
    )


def test_partial_finalization_evidence_is_preserved_as_unknown(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-partial", operation_kind="RECONCILE", snapshot_revision=1
    )
    payload = json.loads(job.payload_json)
    payload["finalization_evidence"] = {
        "operation_id": "op-partial",
        "candidate_fingerprint": "projection-v1",
    }
    job.status = JobStatus.RUNNING
    job.payload_json = json.dumps(payload, sort_keys=True)
    db.commit()

    result = _mark_unknown(
        db,
        job.id,
        operation_id="op-partial",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
        diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
    )

    assert result.finalized is False
    assert result.retryable is False
    current = db.get(Job, job.id)
    assert current is not None
    assert current.status is JobStatus.RUNNING
    assert (
        json.loads(current.payload_json)["finalization_evidence"]
        == payload["finalization_evidence"]
    )
    assert current.last_error_code is None


@pytest.mark.parametrize(
    "state", ["missing", "malformed", "partial", "wrong-fingerprint", "wrong-identity"]
)
def test_ambiguous_finalization_persists_global_blocker(db: Session, state: str) -> None:
    job_id = 9001
    if state != "missing":
        job = enqueue_mihomo_reconciliation(
            db, operation_id="op-ambiguous", operation_kind="RECONCILE", snapshot_revision=1
        )
        job_id = job.id
        if state == "malformed":
            job.payload_json = "not-json"
        else:
            payload = json.loads(job.payload_json)
            if state == "wrong-identity":
                payload["operation_id"] = "different-operation"
            payload["finalization_evidence"] = {
                "operation_id": (
                    "different-operation" if state == "wrong-identity" else "op-ambiguous"
                ),
                "operation_kind": "RECONCILE",
                "snapshot_revision": 1,
                "candidate_fingerprint": (
                    "wrong" if state == "wrong-fingerprint" else "projection-v1"
                ),
            }
            job.status = JobStatus.SUCCEEDED
            job.payload_json = json.dumps(payload, sort_keys=True)
        db.commit()

    result = _mark_unknown(
        db,
        job_id,
        operation_id="op-ambiguous",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
        diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
        applied=False,
    )

    assert result.retryable is False
    blocker = db.scalar(select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE))
    assert blocker is not None
    if state != "missing":
        current = db.get(Job, job_id)
        assert current is not None
        if state == "malformed":
            assert current.payload_json == "not-json"
        else:
            assert current.status is JobStatus.SUCCEEDED


def test_exact_landed_and_exact_running_need_no_duplicate_blocker(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-exact", operation_kind="RECONCILE", snapshot_revision=1
    )
    payload = json.loads(job.payload_json)
    payload["finalization_evidence"] = {
        "operation_id": "op-exact",
        "operation_kind": "RECONCILE",
        "snapshot_revision": 1,
        "candidate_fingerprint": "projection-v1",
        "verification": "readback-v1",
    }
    job.status = JobStatus.SUCCEEDED
    job.payload_json = json.dumps(payload, sort_keys=True)
    ensure_mihomo_blocker(
        db,
        blocker_kind=MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
        source_job_id=job.id,
        operation_id="op-exact",
        snapshot_revision=1,
        reason_code=MIHOMO_LOCK_RELEASE_PENDING,
    )
    db.commit()
    landed = _mark_unknown(
        db,
        job.id,
        operation_id="op-exact",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
        diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
        applied=False,
    )
    assert landed.finalized is True
    release_blocker = db.scalar(
        select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)
    )
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING

    running_job = enqueue_mihomo_reconciliation(
        db, operation_id="op-exact-running", operation_kind="RECONCILE", snapshot_revision=1
    )
    running_job.status = JobStatus.RUNNING
    db.commit()
    running = _mark_unknown(
        db,
        running_job.id,
        operation_id="op-exact-running",
        operation_kind="RECONCILE",
        snapshot_revision=1,
        candidate_fingerprint="projection-v1",
        diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
        applied=False,
    )
    assert running.finalized is False
    assert (
        db.scalar(
            select(Job).where(
                Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
                Job.dedupe_key == f"MIHOMO_BLOCKER:FINALIZATION:{running_job.id}",
            )
        )
        is None
    )


def test_ack_loss_after_durable_commit_is_landed_without_downgrade(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-ack-loss", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def commit_then_lose_ack() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")
        original_commit()

    monkeypatch.setattr(db, "commit", commit_then_lose_ack)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result == MihomoReconciliationResult(1, True, True, False)
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.last_error_code is None
    release_blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING


def test_unknown_marker_precommit_failure_keeps_release_fallback_and_blocks_newer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-r5-marker-before", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def fail_marker_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("marker commit failed before durable write")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_marker_commit)
    with pytest.raises(reconciliation.MihomoCommitOutcomeUnknown):
        _reconcile_with_fake_lock(
            db,
            provider,
            _Verifier(RuntimeError("verifier outcome unknown")),
            monkeypatch,
            durable_fallback=True,
        )

    source = db.get(Job, 1)
    assert source is not None
    assert source.status is JobStatus.RUNNING
    assert source.last_error_code is None
    blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert blocker is not None
    assert blocker.status is JobStatus.PENDING
    assert blocker.last_error_code == MIHOMO_DURABLE_STATE_UNKNOWN

    enqueue_mihomo_reconciliation(
        db,
        operation_id="op-r5-marker-before-newer",
        operation_kind="RECONCILE",
        snapshot_revision=1,
    )
    db.commit()
    newer_provider = _Provider()
    assert (
        _reconcile_with_fake_lock(
            db,
            newer_provider,
            _Verifier(ProjectionVerification(True, "readback-v1")),
            monkeypatch,
        )
        is None
    )
    assert newer_provider.apply_count == 0


def test_unknown_marker_ack_loss_is_not_retryable_and_keeps_release_fallback(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-r5-marker-ack", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def commit_then_lose_marker_ack() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_commit()
            raise RuntimeError("marker commit acknowledgement lost")
        original_commit()

    monkeypatch.setattr(db, "commit", commit_then_lose_marker_ack)
    with pytest.raises(reconciliation.MihomoCommitOutcomeUnknown):
        _reconcile_with_fake_lock(
            db,
            provider,
            _Verifier(RuntimeError("verifier outcome unknown")),
            monkeypatch,
            durable_fallback=True,
        )

    source = db.get(Job, 1)
    assert source is not None
    assert source.status is JobStatus.RUNNING
    assert source.last_error_code == MIHOMO_RUNTIME_UNKNOWN
    blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert blocker is not None
    assert blocker.status is JobStatus.PENDING
    assert blocker.last_error_code == MIHOMO_DURABLE_STATE_UNKNOWN


def test_apply_unknown_blocker_commit_failure_uses_release_fallback(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-r5-apply-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_result = SimpleNamespace(applied=False)
    original_commit = db.commit
    calls = 0

    def fail_finalization_blocker_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("finalization blocker commit failed")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_finalization_blocker_commit)
    with pytest.raises(reconciliation.MihomoCommitOutcomeUnknown):
        _reconcile_with_fake_lock(
            db,
            provider,
            _Verifier(ProjectionVerification(True, "readback-v1")),
            monkeypatch,
            durable_fallback=True,
        )

    source = db.get(Job, 1)
    assert source is not None
    assert source.status is JobStatus.RUNNING
    assert source.last_error_code is None
    release_blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING
    assert release_blocker.last_error_code == MIHOMO_DURABLE_STATE_UNKNOWN


def test_rollback_unknown_marker_persistence_failure_is_not_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db,
        operation_id="op-r5-rollback-unknown",
        operation_kind="RECONCILE",
        snapshot_revision=1,
    )
    db.commit()
    provider = _Provider()
    provider.apply_error = MihomoRollbackUnknownError()
    original_commit = db.commit
    calls = 0

    def fail_rollback_marker_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("rollback unknown marker commit failed")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_rollback_marker_commit)
    with pytest.raises(reconciliation.MihomoCommitOutcomeUnknown):
        _reconcile_with_fake_lock(
            db,
            provider,
            _Verifier(ProjectionVerification(True, "readback-v1")),
            monkeypatch,
            durable_fallback=True,
        )

    source = db.get(Job, 1)
    assert source is not None
    assert source.status is JobStatus.RUNNING
    assert source.last_error_code is None
    release_blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING
    assert release_blocker.last_error_code == MIHOMO_DURABLE_STATE_UNKNOWN


def test_unknown_current_read_failure_becomes_typed_and_keeps_fallback(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-r5-read-failure", operation_kind="RECONCILE", snapshot_revision=1
    )
    job.status = JobStatus.RUNNING
    db.commit()
    monkeypatch.setattr(
        reconciliation,
        "classify_finalization_outcome",
        lambda *args, **kwargs: CommitOutcome.UNKNOWN,
    )
    original_scalar = cast(Callable[..., object], db.scalar)
    failed = False

    def fail_current_read(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("current read failed")
        return original_scalar(*args, **kwargs)

    monkeypatch.setattr(db, "scalar", fail_current_read)

    db.info["mihomo_projection_release_metadata"] = {
        "source_job_id": job.id,
        "operation_id": "op-r5-read-failure",
        "snapshot_revision": 1,
    }
    with (
        pytest.raises(reconciliation.MihomoCommitOutcomeUnknown),
        _fake_lock_with_durable_fallback(db),
    ):
        _mark_unknown(
            db,
            job.id,
            operation_id="op-r5-read-failure",
            operation_kind="RECONCILE",
            snapshot_revision=1,
            candidate_fingerprint="projection-v1",
            diagnostic=MIHOMO_FINALIZATION_UNKNOWN,
        )

    assert db.info.get(SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY) is None
    release_blocker = db.get(Job, 2)
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING
    assert release_blocker.last_error_code == MIHOMO_DURABLE_STATE_UNKNOWN


def test_commit_before_durable_write_is_absent_and_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-before-commit", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def fail_final_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("commit failed before durable write")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_final_commit)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.finalized is False
    assert result.retryable is True
    assert result.reason_code == "MIHOMO_RECONCILIATION_RUNTIME_FAILURE"
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert (
        db.scalar(
            select(Job).where(
                Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
                Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
            )
        )
        is None
    )


def test_observer_unavailable_after_ack_loss_preserves_exact_landed_row(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-observer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def commit_then_observer_fails() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_commit()
            raise RuntimeError("acknowledgement lost")
        original_commit()

    def unavailable(_db: Session) -> object:
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(db, "commit", commit_then_observer_fails)
    monkeypatch.setattr(reconciliation, "_engine", unavailable)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result == MihomoReconciliationResult(1, True, True, False)
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.last_error_code is None


def test_observer_unavailable_with_exact_running_row_is_manual_not_failed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-observer-running", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    original_commit = db.commit
    calls = 0

    def fail_final_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("commit acknowledgement unavailable")
        original_commit()

    def unavailable(_db: Session) -> object:
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(db, "commit", fail_final_commit)
    monkeypatch.setattr(reconciliation, "_engine", unavailable)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.finalized is False
    assert result.retryable is False
    assert result.reason_code == MIHOMO_FINALIZATION_UNKNOWN
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == MIHOMO_FINALIZATION_UNKNOWN


@pytest.mark.parametrize(
    "evidence_version",
    ["", " readback-v1", "readback-v1 ", "readback\nv1", "x" * 65, None, 1],
)
def test_evidence_version_is_canonical_and_secret_neutral(
    db: Session,
    evidence_version: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-evidence", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    verification = ProjectionVerification(True, cast(str, evidence_version))
    result = _reconcile_with_fake_lock(db, provider, _Verifier(verification), monkeypatch)

    assert result is not None
    assert result.finalized is False
    assert result.retryable is False
    assert result.reason_code == MIHOMO_RUNTIME_UNKNOWN
    job = db.get(Job, 1)
    assert job is not None
    assert "never-store" not in job.payload_json
    assert "finalization_evidence" not in job.payload_json


def test_rollback_verified_failure_is_bounded_retryable(db: Session) -> None:
    job = enqueue_mihomo_reconciliation(
        db, operation_id="op-retry", operation_kind="RECONCILE", snapshot_revision=1
    )
    job.status = JobStatus.RUNNING
    job.attempts = job.max_attempts
    db.commit()

    result = _failure(db, job.id, MihomoRollbackVerifiedError(), datetime.now(UTC))

    assert result.retryable is False
    assert result.reason_code == "MIHOMO_ROLLBACK_VERIFIED"
    current = db.get(Job, job.id)
    assert current is not None
    assert current.status is JobStatus.FAILED
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="op-retry-newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


def test_stale_desired_snapshot_never_renders_or_applies(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaleLoader:
        def load(self, db: Session, job: Job) -> DesiredForwarderState:
            return DesiredForwarderState(snapshot_revision=2, snapshot_identity="db-v2")

    enqueue_mihomo_reconciliation(
        db, operation_id="op-stale", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    db.info["mihomo_projection_lock_held"] = True
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        StaleLoader(),
        _unused_finalization_resolvers(),
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_STALE_DESIRED_SNAPSHOT"
    assert provider.render_count == 0
    assert provider.apply_count == 0


def test_stale_transport_materialization_fails_before_runtime_mutation(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaleMaterializationLoader:
        def load(self, db: Session, job: Job) -> DesiredForwarderState:
            stale = TransportMaterialization(
                owner_record_id=1,
                provider_code="subscription",
                source_revision=1,
                cache_identity="cache-v1",
                content_hash="not-used-after-stale-check",
                freshness_deadline=datetime.now(UTC) - timedelta(seconds=1),
                content={"proxies": []},
            )
            reference = TransportMaterializationReference(
                owner_record_id=1,
                provider_code="subscription",
                source_revision=1,
                cache_identity="cache-v1",
            )
            return DesiredForwarderState(
                listener_specs=(),
                rules=(),
                transport_materializations=(stale,),
                transport_references=(reference,),
                snapshot_revision=1,
                snapshot_identity="db-v1",
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                },
            )

    enqueue_mihomo_reconciliation(
        db, operation_id="op-stale-material", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = MihomoForwarderProvider(cast(MihomoRuntime, object()))
    db.info["mihomo_projection_lock_held"] = True
    monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    result = reconcile_mihomo_job(
        cast(reconciliation.MihomoProjectionProvider, provider),
        StaleMaterializationLoader(),
        _unused_finalization_resolvers(),
        cast(ProjectionVerifier, _Verifier(ProjectionVerification(True, "readback-v1"))),
        db_factory=lambda: db,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_TRANSPORT_CACHE_STALE"
    assert result.retryable is True


class _Provider:
    def __init__(self) -> None:
        self.render_count = 0
        self.finalize_count = 0
        self.apply_count = 0
        self.apply_error: Exception | None = None
        self.apply_result: object = SimpleNamespace(applied=True)

    def render(self, desired: DesiredForwarderState) -> object:
        self.render_count += 1
        return SimpleNamespace(version="projection-v1")

    def finalize(self, template: object, resolver: object) -> ProjectionCandidate:
        self.finalize_count += 1
        return _Candidate()

    def apply(self, candidate: ProjectionCandidate) -> object:
        self.apply_count += 1
        if self.apply_error is not None:
            raise self.apply_error
        return self.apply_result


class _Candidate:
    version = "projection-v1"
    content = {"secret": "never-store"}


def _unused_finalization_resolvers() -> MihomoFinalizationResolvers:
    class UnusedController:
        def resolve(self, _ref: str) -> None:
            pytest.fail("controller resolver should not be called")

    class UnusedEgress:
        def resolve_egress_credential(self, _ref: str) -> EgressCredentialSnapshot:
            pytest.fail("egress resolver should not be called")

    return MihomoFinalizationResolvers(
        controller=cast(ControllerSecretResolver, UnusedController()),
        egress=cast(EgressCredentialResolver, UnusedEgress()),
    )


class _Loader:
    def load(self, db: Session, job: Job) -> DesiredForwarderState:
        return DesiredForwarderState(snapshot_revision=1, snapshot_identity="db-v1")


class _Verifier:
    def __init__(self, result: object) -> None:
        self.result = result

    def verify(
        self, candidate: object, *, operation_id: str, snapshot_revision: int
    ) -> ProjectionVerification | object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _reconcile_with_fake_lock(
    db: Session,
    provider: _Provider,
    verifier: _Verifier,
    monkeypatch: pytest.MonkeyPatch,
    *,
    durable_fallback: bool = False,
) -> MihomoReconciliationResult | None:
    db.info["mihomo_projection_lock_held"] = True
    if durable_fallback:
        monkeypatch.setattr(
            reconciliation,
            "mihomo_projection_write",
            lambda db: _fake_lock_with_durable_fallback(db),
        )
    else:
        monkeypatch.setattr(reconciliation, "mihomo_projection_write", lambda db: nullcontext())
    return reconcile_mihomo_job(
        provider,
        _Loader(),
        _unused_finalization_resolvers(),
        cast(ProjectionVerifier, verifier),
        db_factory=lambda: db,
    )


@contextmanager
def _fake_lock_with_durable_fallback(db: Session) -> Generator[None, None, None]:
    previous = bool(db.info.get("mihomo_projection_lock_held", False))
    db.info["mihomo_projection_lock_held"] = True
    try:
        yield
    finally:
        metadata = db.info.get("mihomo_projection_release_metadata")
        if isinstance(metadata, dict):
            db.rollback()
            blocker = ensure_mihomo_blocker(
                db,
                blocker_kind=MIHOMO_BLOCKER_KIND_LOCK_RELEASE,
                source_job_id=metadata.get("source_job_id"),
                operation_id=metadata.get("operation_id"),
                snapshot_revision=metadata.get("snapshot_revision"),
                reason_code=MIHOMO_LOCK_RELEASE_PENDING,
            )
            blocker.last_error_code = MIHOMO_DURABLE_STATE_UNKNOWN
            db.commit()
        db.info["mihomo_projection_lock_held"] = previous
        db.info.pop("mihomo_projection_release_metadata", None)
        db.info.pop(SESSION_INFO_DURABLE_STATE_UNKNOWN_KEY, None)


def test_positive_verification_finalizes_with_secret_neutral_version(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-3", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )
    assert result is not None
    assert result.finalized is True
    job = db.get(Job, 1)
    assert job is not None
    assert "never-store" not in job.payload_json
    assert "projection-v1" in job.payload_json


def test_finalization_boundary_survives_crash_before_release_and_blocks_new_worker(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-crash-before-release", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.finalized is True
    source = db.get(Job, 1)
    assert source is not None
    assert source.status is JobStatus.SUCCEEDED
    release_blocker = db.scalar(
        select(Job).where(
            Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE,
            Job.dedupe_key == "MIHOMO_BLOCKER:LOCK_RELEASE:1",
        )
    )
    assert release_blocker is not None
    assert release_blocker.status is JobStatus.PENDING

    observer = Session(bind=db.get_bind(), expire_on_commit=False)
    try:
        newer = enqueue_mihomo_reconciliation(
            observer,
            operation_id="op-crash-before-release-newer",
            operation_kind="RECONCILE",
            snapshot_revision=1,
        )
        observer.commit()
        newer_provider = _Provider()
        newer_result = _reconcile_with_fake_lock(
            observer,
            newer_provider,
            _Verifier(ProjectionVerification(True, "readback-v1")),
            monkeypatch,
        )
        assert newer_result is None
        assert newer_provider.apply_count == 0
        newer_row = observer.get(Job, newer.id)
        assert newer_row is not None
        assert newer_row.status is JobStatus.PENDING
    finally:
        observer.close()


def test_nonpositive_apply_result_persists_runtime_blocker_and_blocks_newer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-apply-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_result = SimpleNamespace(applied=False)
    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.retryable is False
    assert result.reason_code == MIHOMO_RUNTIME_UNKNOWN
    assert (
        db.scalar(select(Job).where(Job.job_type == MIHOMO_RECONCILE_BLOCKER_JOB_TYPE)) is not None
    )
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="op-apply-unknown-newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


def test_rollback_verified_is_failed_and_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-verified", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_error = MihomoRollbackVerifiedError()

    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.reason_code == "MIHOMO_ROLLBACK_VERIFIED"
    assert result.retryable is True
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.FAILED


def test_rollback_unknown_is_manual_nonreclaimable_and_blocks_newer_writer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-unknown", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    provider.apply_error = MihomoRollbackUnknownError()

    result = _reconcile_with_fake_lock(
        db,
        provider,
        _Verifier(ProjectionVerification(True, "readback-v1")),
        monkeypatch,
    )

    assert result is not None
    assert result.reason_code == MIHOMO_ROLLBACK_UNKNOWN
    assert result.retryable is False
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == MIHOMO_ROLLBACK_UNKNOWN
    from backend.app.infra.mihomo_reconciliation import _claim

    job.locked_at = datetime.now(UTC) - timedelta(minutes=20)
    job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert _claim(db, datetime.now(UTC)) is None
    newer = enqueue_mihomo_reconciliation(
        db, operation_id="op-rollback-newer", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    db.info["mihomo_projection_lock_held"] = True
    with pytest.raises(MihomoReconciliationBlocked):
        assert_no_unresolved_mihomo_mutation(db, owning_job_id=newer.id)


@pytest.mark.parametrize(
    "verification",
    [ProjectionVerification(False, "readback-v1"), None, ValueError("observer unavailable")],
)
def test_post_apply_verification_uncertainty_is_manual_and_blocking(
    db: Session, verification: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue_mihomo_reconciliation(
        db, operation_id="op-4", operation_kind="RECONCILE", snapshot_revision=1
    )
    db.commit()
    provider = _Provider()
    result = _reconcile_with_fake_lock(db, provider, _Verifier(verification), monkeypatch)
    assert result is not None
    assert result.retryable is False
    assert result.finalized is False
    assert result.reason_code == "MIHOMO_RUNTIME_PROJECTION_UNKNOWN"
    job = db.get(Job, 1)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error_code == "MIHOMO_RUNTIME_PROJECTION_UNKNOWN"
    assert provider.apply_count == 1
