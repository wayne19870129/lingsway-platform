from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
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
    GatewayReconciliationBlocked,
    assert_no_unresolved_gateway_mutation,
    enqueue_gateway_reconciliation,
)
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.infra.xray_reality import (
    REALITY_IDENTITY_PURPOSE,
    REALITY_IDENTITY_SECRET_REF,
    bootstrap_reality_identity,
)
from backend.app.models import (
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    JobStatus,
    Order,
    Plan,
    Secret,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.base import EgressEndpointDTO
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider


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
        with gateway_route_binding_write(writer_c):
            with pytest.raises(GatewayReconciliationBlocked):
                assert_no_unresolved_gateway_mutation(writer_c)
