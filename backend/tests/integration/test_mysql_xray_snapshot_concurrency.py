from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.infra.xray_reality as reality_bootstrap
import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.core.secrets import put_secret
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.gateway_reconciliation import (
    FinalizationCommitOutcome,
    FirstCommitOutcome,
    GatewayReconciliationBlocked,
    GatewayReconciliationResult,
    assert_no_unresolved_gateway_mutation,
    classify_finalization_outcome,
    classify_first_commit_outcome,
    enqueue_gateway_reconciliation,
    reconcile_gateway_job,
)
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.infra.xray_reality import (
    REALITY_IDENTITY_PURPOSE,
    REALITY_IDENTITY_SECRET_REF,
    bootstrap_reality_identity,
)
from backend.app.models import (
    AuditLog,
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    Plan,
    Secret,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    EgressEndpointDTO,
    ValidationResult,
)
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider
from backend.app.workers.accounting_sync import release_egress


@pytest.fixture
def mysql_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for the MySQL current-read test")
    sync_url = database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    engine = build_engine(sync_url)
    if engine.dialect.name != "mysql":
        engine.dispose()
        pytest.skip("the current-read concurrency contract is MySQL-only")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(engine: Engine) -> tuple[int, int, EgressEndpoint, EgressEndpoint]:
    suffix = uuid4().hex[:10]
    with Session(engine, expire_on_commit=False) as db:
        plan = Plan(
            plan_code=f"SNAP-{suffix}",
            name="snapshot test",
            traffic_limit_bytes=50 * 1024**3,
            duration_days=30,
            price="30.00",
            currency="USD",
            route_group_code=f"SNAP-RG-{suffix}",
        )
        group = EgressGroup(code=f"SNAP-EG-{suffix}", region="US")
        customer_a = Customer(
            customer_no=f"SNAP-A-{suffix}",
            email=f"snapshot-a-{suffix}@example.invalid",
            password_hash="hash",
        )
        customer_b = Customer(
            customer_no=f"SNAP-B-{suffix}",
            email=f"snapshot-b-{suffix}@example.invalid",
            password_hash="hash",
        )
        db.add_all([plan, group, customer_a, customer_b])
        db.flush()
        order_a = Order(
            order_no=f"SNAP-OA-{suffix}",
            customer_id=customer_a.id,
            plan_id=plan.id,
            client_request_id=f"snap-oa-{suffix}",
            order_type="PURCHASE",
            amount="30.00",
            currency="USD",
        )
        order_b = Order(
            order_no=f"SNAP-OB-{suffix}",
            customer_id=customer_b.id,
            plan_id=plan.id,
            client_request_id=f"snap-ob-{suffix}",
            order_type="PURCHASE",
            amount="30.00",
            currency="USD",
        )
        db.add_all([order_a, order_b])
        db.flush()
        sub_a = Subscription(
            subscription_no=f"SNAP-SA-{suffix}",
            customer_id=customer_a.id,
            plan_id=plan.id,
            order_id=order_a.id,
            status=SubscriptionStatus.PROVISIONING,
            route_group_code=plan.route_group_code,
            service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        sub_b = Subscription(
            subscription_no=f"SNAP-SB-{suffix}",
            customer_id=customer_b.id,
            plan_id=plan.id,
            order_id=order_b.id,
            status=SubscriptionStatus.PROVISIONING,
            route_group_code=plan.route_group_code,
            service_expire_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        endpoint_a = EgressEndpoint(
            group_id=group.id,
            code=f"SNAP-A-{suffix}",
            provider_name="webshare",
            host="a-before.example.invalid",
            port=1080,
            protocol="socks5",
            credential_secret_ref=f"snap/endpoint-a/{suffix}",
            mihomo_listen_port=21080 + int(suffix[:3], 16) % 1000,
        )
        endpoint_b = EgressEndpoint(
            group_id=group.id,
            code=f"SNAP-B-{suffix}",
            provider_name="webshare",
            host="b-before.example.invalid",
            port=1081,
            protocol="socks5",
            credential_secret_ref=f"snap/endpoint-b/{suffix}",
            mihomo_listen_port=22080 + int(suffix[3:6], 16) % 1000,
        )
        db.add_all([sub_a, sub_b, endpoint_a, endpoint_b])
        db.flush()
        db.commit()
        return sub_a.id, sub_b.id, endpoint_a, endpoint_b


def _configured_provider() -> XrayFileProvider:
    return XrayFileProvider(
        runtime=None,  # type: ignore[arg-type]
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="shared.example:443",
            reality_server_names=("shared.example",),
        ),
    )


def test_mysql_snapshot_sees_b_committed_and_a_uncommitted_with_one_session(
    mysql_engine: Engine,
) -> None:
    """Prove current-read freshness without committing A before render.

    A first performs an ordinary consistent read. B then commits a route,
    endpoint update, binding override, and Secret while holding the named
    writer lock. A acquires that lock afterwards, adds its own uncommitted
    route and Secret, and must render both customers from one Session.
    """
    sub_a, sub_b, endpoint_a, endpoint_b = _seed(mysql_engine)
    with Session(mysql_engine) as db:
        put_secret(
            db,
            "gateway/xray/reality-identity",
            '{"privateKey":"persisted-private-key","shortIds":["persisted-id"]}',
            "XRAY_REALITY_IDENTITY",
        )
        db.commit()

    with Session(mysql_engine) as db_a:
        # Establish the old REPEATABLE READ view before B commits.
        assert db_a.scalars(select(GatewayRouteBinding)).all() == []

        with Session(mysql_engine) as db_b:
            endpoint_b = db_b.get(EgressEndpoint, endpoint_b.id)
            assert endpoint_b is not None
            endpoint_b.host = "b-after.example.invalid"
            put_secret(
                db_b,
                "snap/b-credential",
                '{"username":"b-user","password":"b-password"}',
                "EGRESS_CREDENTIAL",
            )
            db_b.add(
                EgressBinding(
                    subscription_id=sub_b,
                    egress_id=endpoint_b.id,
                    credential_secret_ref="snap/b-credential",
                )
            )
            db_b.add(
                GatewayRouteBinding(
                    subscription_id=sub_b,
                    egress_id=endpoint_b.id,
                    gateway_principal="b-principal",
                    outbound_tag="egress-b",
                    enabled=True,
                )
            )
            with gateway_route_binding_write(db_b):
                db_b.commit()

        with gateway_route_binding_write(db_a):
            put_secret(
                db_a,
                "snap/a-credential",
                '{"username":"a-user","password":"a-password"}',
                "EGRESS_CREDENTIAL",
            )
            state = SqlAlchemyProvisioningState(db_a, sub_a)
            db_a.add(
                EgressBinding(
                    subscription_id=sub_a,
                    egress_id=endpoint_a.id,
                    credential_secret_ref="snap/a-credential",
                )
            )
            dto = EgressEndpointDTO(
                str(endpoint_a.id), endpoint_a.host, endpoint_a.port, endpoint_a.protocol
            )
            desired = state.desired_routing_state(
                request=None,  # type: ignore[arg-type]
                endpoint=dto,
                tenant=None,  # type: ignore[arg-type]
                routing_principal="a-principal",
            )
            candidate = _configured_provider().render(
                desired, SqlAlchemyCredentialResolver(db_a)
            )  # type: ignore[arg-type]

            assert {outbound.tag for outbound in desired.outbounds} == {
                "egress-1",
                "egress-b",
            }
            assert desired.outbounds[1].host == "b-after.example.invalid"
            assert candidate.content["outbounds"][1]["settings"]["servers"][0]["users"] == [
                {"username": "a-user", "password": "a-password"}
            ]  # type: ignore[index]
            assert candidate.content["outbounds"][2]["settings"]["servers"][0]["users"] == [
                {"username": "b-user", "password": "b-password"}
            ]  # type: ignore[index]
            assert db_a.in_transaction()
            with Session(mysql_engine) as observer:
                assert observer.scalar(
                    select(GatewayRouteBinding).where(
                        GatewayRouteBinding.subscription_id == sub_a
                    )
                ) is None
            db_a.rollback()


def test_mysql_snapshot_does_not_lock_unrelated_phase_a_binding(
    mysql_engine: Engine,
) -> None:
    """Guard the ADR-017 lock order for a binding with no active route yet.

    B holds an uncommitted Phase-A-like endpoint/binding/Secret write but no
    named lock and no committed route. A then holds the named lock and builds
    its own snapshot. The snapshot must not lock B's unrelated subscription;
    after A rolls back and releases the lock, B must still acquire it and
    commit its route.
    """
    sub_a, sub_b, endpoint_a, endpoint_b = _seed(mysql_engine)

    with Session(mysql_engine) as db_b, Session(mysql_engine) as db_a:
        db_a.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))

        endpoint_b = db_b.get(EgressEndpoint, endpoint_b.id)
        assert endpoint_b is not None
        endpoint_b.host = "b-phase-a.example.invalid"
        put_secret(
            db_b,
            "snap/unrelated-phase-a-credential",
            '{"username":"unrelated-user","password":"unrelated-password"}',
            "EGRESS_CREDENTIAL",
        )
        db_b.add(
            EgressBinding(
                subscription_id=sub_b,
                egress_id=endpoint_b.id,
                credential_secret_ref="snap/unrelated-phase-a-credential",
            )
        )
        db_b.flush()

        with gateway_route_binding_write(db_a):
            put_secret(
                db_a,
                "snap/own-lock-order-credential",
                '{"username":"own-user","password":"own-password"}',
                "EGRESS_CREDENTIAL",
            )
            state = SqlAlchemyProvisioningState(db_a, sub_a)
            db_a.add(
                EgressBinding(
                    subscription_id=sub_a,
                    egress_id=endpoint_a.id,
                    credential_secret_ref="snap/own-lock-order-credential",
                )
            )
            desired = state.desired_routing_state(
                request=None,  # type: ignore[arg-type]
                endpoint=EgressEndpointDTO(
                    str(endpoint_a.id), endpoint_a.host, endpoint_a.port, endpoint_a.protocol
                ),
                tenant=None,  # type: ignore[arg-type]
                routing_principal="own-principal",
            )
            assert {outbound.tag for outbound in desired.outbounds} == {"egress-1"}
            assert desired.outbounds[0].credential_secret_ref == "snap/own-lock-order-credential"
            assert db_a.in_transaction()
            db_a.rollback()

        with gateway_route_binding_write(db_b, timeout_seconds=2):
            db_b.add(
                GatewayRouteBinding(
                    subscription_id=sub_b,
                    egress_id=endpoint_b.id,
                    gateway_principal="unrelated-principal",
                    outbound_tag="egress-b-after-lock",
                    enabled=True,
                )
            )
            db_b.commit()

        with Session(mysql_engine) as verify_db:
            assert verify_db.scalar(
                select(GatewayRouteBinding).where(
                    GatewayRouteBinding.subscription_id == sub_b
                )
            ) is not None


def test_mysql_snapshot_refreshes_stale_identity_map_rows(mysql_engine: Engine) -> None:
    """Prove current reads refresh stale route, endpoint, binding, and Secret state."""
    sub_a, _, endpoint_a, _ = _seed(mysql_engine)
    initial_ref = f"snap/initial/{uuid4().hex[:10]}"

    with Session(mysql_engine) as seed_db:
        put_secret(
            seed_db,
            initial_ref,
            '{"username":"old-user","password":"old-password"}',
            "EGRESS_CREDENTIAL",
        )
        seed_db.add(
            EgressBinding(
                subscription_id=sub_a,
                egress_id=endpoint_a.id,
                credential_secret_ref=initial_ref,
            )
        )
        seed_db.add(
            GatewayRouteBinding(
                subscription_id=sub_a,
                egress_id=endpoint_a.id,
                gateway_principal="old-principal",
                outbound_tag="egress-initial",
                enabled=True,
            )
        )
        with gateway_route_binding_write(seed_db):
            seed_db.commit()

    with Session(mysql_engine) as db_a:
        stale_route = db_a.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == sub_a
            )
        )
        stale_endpoint = db_a.get(EgressEndpoint, endpoint_a.id)
        stale_binding = db_a.scalar(
            select(EgressBinding).where(EgressBinding.subscription_id == sub_a)
        )
        assert stale_route is not None
        assert stale_endpoint is not None
        assert stale_binding is not None
        assert stale_route.gateway_principal == "old-principal"
        assert stale_endpoint.host == "a-before.example.invalid"
        assert stale_binding.credential_secret_ref == initial_ref

        with Session(mysql_engine) as db_b, gateway_route_binding_write(db_b):
            route_b = db_b.scalar(
                select(GatewayRouteBinding).where(
                    GatewayRouteBinding.subscription_id == sub_a
                )
            )
            endpoint_b = db_b.get(EgressEndpoint, endpoint_a.id)
            binding_b = db_b.scalar(
                select(EgressBinding).where(EgressBinding.subscription_id == sub_a)
            )
            assert route_b is not None
            assert endpoint_b is not None
            assert binding_b is not None
            route_b.gateway_principal = "fresh-principal"
            route_b.outbound_tag = "egress-fresh"
            endpoint_b.host = "a-after.example.invalid"
            endpoint_b.port = 2080
            fresh_ref = "snap/fresh-binding"
            binding_b.credential_secret_ref = fresh_ref
            put_secret(
                db_b,
                fresh_ref,
                '{"username":"fresh-user","password":"fresh-password"}',
                "EGRESS_CREDENTIAL",
            )
            db_b.commit()

        with gateway_route_binding_write(db_a):
            desired = SqlAlchemyProvisioningState(
                db_a, sub_a
            )._full_desired_routing_snapshot()
            credential = SqlAlchemyCredentialResolver(db_a).resolve(
                desired.outbounds[0].credential_secret_ref
            )

            assert desired.user_routes == {"fresh-principal": "egress-fresh"}
            assert desired.outbounds[0].host == "a-after.example.invalid"
            assert desired.outbounds[0].port == 2080
            assert desired.outbounds[0].credential_secret_ref == "snap/fresh-binding"
            assert credential.username == "fresh-user"
            assert credential.password == "fresh-password"
            db_a.rollback()


def test_mysql_concurrent_reality_bootstrap_keeps_one_identity(
    mysql_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent bootstrap operations converge on one persisted winner."""
    candidates: list[str] = []
    candidate_lock = Lock()
    both_generators_started = Barrier(2)

    def generate(_: str) -> str:
        both_generators_started.wait(timeout=15)
        with candidate_lock:
            candidate = f"candidate-{len(candidates) + 1}"
            candidates.append(candidate)
        return '{"privateKey":"' + candidate + '","shortIds":["shared-id"]}'

    monkeypatch.setattr(reality_bootstrap, "_new_reality_identity", generate)

    def bootstrap() -> XrayRealityConfig:
        with Session(mysql_engine) as db:
            return bootstrap_reality_identity(db, xray_binary="unused-xray")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bootstrap(), range(2)))

    assert len(candidates) == 2
    assert results[0] == results[1]
    with Session(mysql_engine) as db:
        persisted = SqlAlchemyCredentialResolver(db).resolve_reality_identity()
        stored = db.scalars(
            select(Secret).where(
                Secret.secret_ref == REALITY_IDENTITY_SECRET_REF,
                Secret.purpose == REALITY_IDENTITY_PURPOSE,
            )
        ).all()
    assert results[0] == persisted
    assert len(stored) == 1


def test_mysql_reality_current_read_refreshes_stale_identity_map(
    mysql_engine: Engine,
) -> None:
    """A purpose-bound current read returns B's committed identity, not A's stale row."""
    with Session(mysql_engine) as writer:
        put_secret(
            writer,
            REALITY_IDENTITY_SECRET_REF,
            '{"privateKey":"old-private-key","shortIds":["old-id"]}',
            REALITY_IDENTITY_PURPOSE,
        )
        writer.commit()

    with Session(mysql_engine) as reader:
        stale = reader.scalar(
            select(Secret).where(
                Secret.secret_ref == REALITY_IDENTITY_SECRET_REF,
                Secret.purpose == REALITY_IDENTITY_PURPOSE,
            )
        )
        assert stale is not None
        with Session(mysql_engine) as writer:
            put_secret(
                writer,
                REALITY_IDENTITY_SECRET_REF,
                '{"privateKey":"new-private-key","shortIds":["new-id"]}',
                REALITY_IDENTITY_PURPOSE,
            )
            writer.commit()

        refreshed = SqlAlchemyCredentialResolver(reader).resolve_reality_identity()

    assert refreshed == XrayRealityConfig("new-private-key", ("new-id",))



def test_mysql_gateway_pending_current_read_breaks_stale_snapshot(
    mysql_engine: Engine,
) -> None:
    """A phase-A RR snapshot cannot hide a committed pending gateway intent."""
    _, subscription_id, _, _ = _seed(mysql_engine)
    with Session(mysql_engine) as writer_c:
        # This ordinary read deliberately establishes Writer-C's old RR view.
        assert writer_c.scalars(select(GatewayRouteBinding)).all() == []

        with Session(mysql_engine) as writer_b:
            with gateway_route_binding_write(writer_b):
                pending = enqueue_gateway_reconciliation(
                    writer_b,
                    operation_kind="PURCHASE",
                    subscription_id=subscription_id,
                    operation_id=f"stale-{uuid4().hex}",
                )
                writer_b.commit()
            assert pending.status is JobStatus.PENDING

        # The named lock is the single projection-writer boundary. The
        # unresolved query is a locking/current read, so it must see B even
        # though this Session already has an older consistent-read snapshot.
        with gateway_route_binding_write(writer_c), pytest.raises(
            GatewayReconciliationBlocked
        ):
            assert_no_unresolved_gateway_mutation(writer_c)


def test_mysql_gateway_dedupe_current_read_does_not_duplicate_intent(
    mysql_engine: Engine,
) -> None:
    """A stale Session cannot enqueue a second row for a committed dedupe key."""
    _, subscription_id, _, _ = _seed(mysql_engine)
    operation_id = f"dedupe-{uuid4().hex}"
    with Session(mysql_engine) as writer_c:
        assert writer_c.scalars(select(GatewayRouteBinding)).all() == []
        with Session(mysql_engine) as writer_b, gateway_route_binding_write(writer_b):
            pending = enqueue_gateway_reconciliation(
                writer_b,
                operation_kind="PURCHASE",
                subscription_id=subscription_id,
                operation_id=operation_id,
            )
            writer_b.commit()
            pending_id = pending.id

        with gateway_route_binding_write(writer_c):
            recovered = enqueue_gateway_reconciliation(
                writer_c,
                operation_kind="PURCHASE",
                subscription_id=subscription_id,
                operation_id=operation_id,
            )
            assert recovered.id == pending_id
            writer_c.rollback()


def test_mysql_first_commit_probe_requires_complete_authoritative_evidence(
    mysql_engine: Engine,
) -> None:
    """Fresh commit classification distinguishes landed, absent, and partial B."""
    _, subscription_id, endpoint, _ = _seed(mysql_engine)
    operation_id = f"certainty-{uuid4().hex}"
    dedupe_key = f"GATEWAY_RECONCILE:PURCHASE:{operation_id}"
    with Session(mysql_engine) as db, gateway_route_binding_write(db):
        enqueue_gateway_reconciliation(
            db,
            operation_kind="PURCHASE",
            subscription_id=subscription_id,
            operation_id=operation_id,
        )
        db.add(
            GatewayRouteBinding(
                subscription_id=subscription_id,
                egress_id=endpoint.id,
                gateway_principal=f"certainty-{operation_id}",
                outbound_tag=f"certainty-{operation_id}",
                enabled=True,
            )
        )
        db.commit()

    with Session(mysql_engine) as probe:
        assert classify_first_commit_outcome(
            probe, dedupe_key=dedupe_key, subscription_id=subscription_id
        ) is FirstCommitOutcome.LANDED

    _, absent_subscription, _, _ = _seed(mysql_engine)
    with Session(mysql_engine) as probe:
        assert classify_first_commit_outcome(
            probe,
            dedupe_key=f"GATEWAY_RECONCILE:PURCHASE:absent-{uuid4().hex}",
            subscription_id=absent_subscription,
        ) is FirstCommitOutcome.ABSENT


class _FakeNotify:
    def send(self, event: object) -> object:
        del event
        return object()


class _FakeGateway:
    """Deterministic gateway double; no external network or Xray process."""

    def __init__(self, *, failures: int = 0, block: Barrier | None = None) -> None:
        self.failures = failures
        self.block = block
        self.apply_calls = 0
        self._calls_lock = Lock()
        self.notify = _FakeNotify()

    def render(self, desired: object, resolver: object) -> CandidateConfig:
        del desired, resolver
        return CandidateConfig({}, "mysql-test")

    def validate(self, candidate: CandidateConfig) -> ValidationResult:
        del candidate
        return ValidationResult(True)

    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        del candidate
        with self._calls_lock:
            self.apply_calls += 1
            call_number = self.apply_calls
        if self.block is not None:
            self.block.wait(timeout=15)
        if call_number <= self.failures:
            raise RuntimeError("deterministic gateway failure")
        return ApplyResult(True, "mysql-test")

    def health(self) -> object:
        return object()


class _FaultSession(Session):
    """Inject a deterministic second-commit failure after claim commit."""

    def __init__(self, bind: Engine, *, fail_mode: str | None = None) -> None:
        super().__init__(bind=bind)
        self._fail_mode = fail_mode
        self._commit_calls = 0

    def commit(self) -> None:
        self._commit_calls += 1
        if self._commit_calls == 2 and self._fail_mode == "before":
            self.rollback()
            raise RuntimeError("injected finalization commit failure")
        super().commit()
        if self._commit_calls == 2 and self._fail_mode == "ack":
            raise RuntimeError("injected finalization commit acknowledgement loss")


def _seed_pending_gateway_case(
    engine: Engine, *, token: str | None = None
) -> tuple[int, int, int, int]:
    """Seed durable DB B, a pending gateway job, and an optional token."""
    subscription_id, _, endpoint, _ = _seed(engine)
    operation_id = f"orchestration-{uuid4().hex}"
    with Session(engine) as db:
        subscription = db.get(Subscription, subscription_id)
        assert subscription is not None
        if token is not None:
            SqlAlchemyProvisioningState(db, subscription_id).store_subscription_token(
                str(subscription.customer_id), token
            )
        credential_ref = f"mysql-test/{uuid4().hex}"
        put_secret(
            db,
            credential_ref,
            '{"username":"mysql-test-user","password":"mysql-test-password"}',
            "EGRESS_CREDENTIAL",
        )
        db.add(
            EgressBinding(
                subscription_id=subscription_id,
                egress_id=endpoint.id,
                credential_secret_ref=credential_ref,
            )
        )
        db.add(
            GatewayRouteBinding(
                subscription_id=subscription_id,
                egress_id=endpoint.id,
                gateway_principal=f"mysql-principal-{operation_id}",
                outbound_tag=f"mysql-egress-{operation_id}",
                enabled=True,
            )
        )
        provision_run = Job(
            job_type="PROVISION",
            dedupe_key=f"PROVISION:{operation_id}",
            status=JobStatus.RUNNING,
            available_at=datetime.now(UTC),
        )
        db.add(provision_run)
        db.flush()
        with gateway_route_binding_write(db):
            job = enqueue_gateway_reconciliation(
                db,
                operation_kind="PURCHASE",
                subscription_id=subscription_id,
                order_id=subscription.order_id,
                provision_run_id=str(provision_run.id),
                operation_id=operation_id,
            )
            job_id = job.id
            run_id = provision_run.id
            order_id = subscription.order_id
            db.commit()
    return subscription_id, order_id, job_id, run_id


def _make_gateway() -> _FakeGateway:
    return _FakeGateway()


def _reconcile(
    engine: Engine, gateway: _FakeGateway
) -> GatewayReconciliationResult | None:
    return reconcile_gateway_job(  # type: ignore[arg-type]
        gateway,
        db_factory=lambda: Session(engine),
    )


def _set_job_available_now(engine: Engine, job_id: int) -> None:
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.available_at = datetime.now(UTC)
        db.commit()


def _assert_purchase_finalized(
    engine: Engine, subscription_id: int, order_id: int, job_id: int, run_id: int
) -> None:
    with Session(engine) as db:
        subscription = db.get(Subscription, subscription_id)
        order = db.get(Order, order_id)
        job = db.get(Job, job_id)
        run = db.get(Job, run_id)
        assert subscription is not None
        assert order is not None
        assert job is not None
        assert run is not None
        assert subscription.status is SubscriptionStatus.ACTIVE
        assert order.status is OrderStatus.ACTIVATED
        assert job.status is JobStatus.SUCCEEDED
        assert run.status is JobStatus.SUCCEEDED


def test_mysql_gateway_crash_before_runtime_apply_recovers(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    gateway = _make_gateway()

    result = _reconcile(mysql_engine, gateway)

    assert result is not None
    assert gateway.apply_calls == 1
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)


def test_mysql_gateway_runtime_failure_then_retry_preserves_db_b(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    gateway = _FakeGateway(failures=1)

    first = _reconcile(mysql_engine, gateway)
    assert first is not None
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        subscription = db.get(Subscription, subscription_id)
        order = db.get(Order, order_id)
        assert job is not None
        assert subscription is not None
        assert order is not None
        assert job.status is JobStatus.FAILED
        assert subscription.status is SubscriptionStatus.PROVISIONING
        assert order.status is OrderStatus.PENDING
    _set_job_available_now(mysql_engine, job_id)

    second = _reconcile(mysql_engine, gateway)

    assert second is not None
    assert gateway.apply_calls == 2
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)


def test_mysql_finalization_commit_failure_is_retryable_without_rollback(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    gateway = _make_gateway()

    first = reconcile_gateway_job(
        gateway,
        db_factory=lambda: _FaultSession(mysql_engine, fail_mode="before"),
    )

    assert first is not None
    assert first.applied is False
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        subscription = db.get(Subscription, subscription_id)
        order = db.get(Order, order_id)
        assert job is not None
        assert subscription is not None
        assert order is not None
        assert job.status is JobStatus.FAILED
        assert subscription.status is SubscriptionStatus.PROVISIONING
        assert order.status is OrderStatus.PENDING
    _set_job_available_now(mysql_engine, job_id)

    second = _reconcile(mysql_engine, gateway)

    assert second is not None
    assert gateway.apply_calls == 2
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)


def test_mysql_finalization_ack_loss_classifies_landed_without_false_failure(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    gateway = _make_gateway()

    result = reconcile_gateway_job(
        gateway,
        db_factory=lambda: _FaultSession(mysql_engine, fail_mode="ack"),
    )

    assert result is not None
    assert result.applied is True
    assert result.finalized is True
    assert result.reason_code is None
    assert gateway.apply_calls == 1
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)
    with Session(mysql_engine) as db:
        subscription = db.get(Subscription, subscription_id)
        assert subscription is not None
        assert subscription.provision_error is None
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.action == "GATEWAY_RECONCILIATION_FAILED",
                AuditLog.entity_id == str(subscription_id),
            )
        ) is None


def test_mysql_finalization_classifier_distinguishes_all_states(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    with Session(mysql_engine) as db:
        assert (
            classify_finalization_outcome(
                db,
                job_id=job_id,
                subscription_id=subscription_id,
                order_id=order_id,
                provision_run_id=run_id,
                operation_kind="PURCHASE",
            )
            is FinalizationCommitOutcome.ABSENT
        )
        job = db.get(Job, job_id)
        subscription = db.get(Subscription, subscription_id)
        order = db.get(Order, order_id)
        run = db.get(Job, run_id)
        assert job is not None
        assert subscription is not None
        assert order is not None
        assert run is not None
        subscription.status = SubscriptionStatus.ACTIVE
        order.status = OrderStatus.ACTIVATED
        job.status = JobStatus.SUCCEEDED
        run.status = JobStatus.SUCCEEDED
        db.commit()
        assert (
            classify_finalization_outcome(
                db,
                job_id=job_id,
                subscription_id=subscription_id,
                order_id=order_id,
                provision_run_id=run_id,
                operation_kind="PURCHASE",
            )
            is FinalizationCommitOutcome.LANDED
        )


def test_mysql_stale_running_gateway_job_is_recovered(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING
        job.locked_at = datetime.now(UTC) - timedelta(minutes=30)
        db.commit()

    gateway = _make_gateway()
    result = _reconcile(mysql_engine, gateway)

    assert result is not None
    assert gateway.apply_calls == 1
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)


def test_mysql_gateway_retry_exhaustion_blocks_new_writer(
    mysql_engine: Engine,
) -> None:
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(mysql_engine)
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.max_attempts = 2
        db.commit()
    gateway = _FakeGateway(failures=10)

    first = _reconcile(mysql_engine, gateway)
    assert first is not None
    _set_job_available_now(mysql_engine, job_id)
    second = _reconcile(mysql_engine, gateway)

    assert second is not None
    assert gateway.apply_calls == 2
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.attempts == 2
        assert job.last_error_code == "GATEWAY_RECONCILIATION_RUNTIMEERROR"
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.action == "GATEWAY_RECONCILIATION_FAILED",
                AuditLog.result == "MANUAL",
                AuditLog.entity_id == str(subscription_id),
            )
        ) is not None
        with gateway_route_binding_write(db), pytest.raises(
            GatewayReconciliationBlocked
        ):
            assert_no_unresolved_gateway_mutation(db)


def test_mysql_two_recovery_workers_apply_once(
    mysql_engine: Engine,
) -> None:
    _, _, job_id, _ = _seed_pending_gateway_case(mysql_engine)
    gateway = _make_gateway()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: _reconcile(mysql_engine, gateway), range(2))
        )

    assert sum(result is not None for result in results) == 1
    assert gateway.apply_calls == 1
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED


def test_mysql_expiry_release_handoff_enqueues_without_runtime_apply(
    mysql_engine: Engine,
) -> None:
    subscription_id, _, endpoint, _ = _seed(mysql_engine)
    with Session(mysql_engine) as db:
        ref = f"release-test/{uuid4().hex}"
        put_secret(
            db,
            ref,
            '{"username":"release-user","password":"release-password"}',
            "EGRESS_CREDENTIAL",
        )
        db.add(
            EgressBinding(
                subscription_id=subscription_id,
                egress_id=endpoint.id,
                credential_secret_ref=ref,
            )
        )
        db.add(
            GatewayRouteBinding(
                subscription_id=subscription_id,
                egress_id=endpoint.id,
                gateway_principal=f"release-principal-{uuid4().hex}",
                outbound_tag=f"release-egress-{uuid4().hex}",
                enabled=True,
            )
        )
        db.commit()
        assert release_egress(db, subscription_id, gateway=None) is False
        binding = db.scalar(
            select(EgressBinding).where(EgressBinding.subscription_id == subscription_id)
        )
        route = db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == subscription_id
            )
        )
        job = db.scalar(
            select(Job).where(Job.job_type == "GATEWAY_RECONCILE")
        )
        assert binding is not None
        assert route is not None
        assert job is not None
        assert binding.released_at is not None
        assert route.enabled is False
        assert route.released_at is not None
        assert job.status is JobStatus.PENDING
        job_id = job.id

    gateway = _make_gateway()
    result = _reconcile(mysql_engine, gateway)

    assert result is not None
    assert gateway.apply_calls == 1
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED


def test_mysql_subscription_token_survives_request_loss_and_recovery(
    mysql_engine: Engine,
) -> None:
    raw_token = f"token-{uuid4().hex}"
    subscription_id, order_id, job_id, run_id = _seed_pending_gateway_case(
        mysql_engine, token=raw_token
    )
    with Session(mysql_engine) as db:
        state = SqlAlchemyProvisioningState(db, subscription_id)
        assert state.read_subscription_token() == raw_token
        assert raw_token in state.current_subscription_url("https://sub.example.invalid")

    gateway = _make_gateway()
    result = _reconcile(mysql_engine, gateway)

    assert result is not None
    _assert_purchase_finalized(mysql_engine, subscription_id, order_id, job_id, run_id)
    with Session(mysql_engine) as db:
        job = db.get(Job, job_id)
        audit = db.scalar(
            select(AuditLog).where(AuditLog.action == "GATEWAY_RECONCILIATION_SUCCEEDED")
        )
        assert job is not None
        assert audit is not None
        assert raw_token not in job.payload_json
        assert raw_token not in audit.detail
        assert raw_token not in (job.last_error_code or "")
