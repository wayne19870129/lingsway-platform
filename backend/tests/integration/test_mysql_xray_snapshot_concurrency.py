from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.core.secrets import put_secret
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    Customer,
    EgressBinding,
    EgressEndpoint,
    EgressGroup,
    GatewayRouteBinding,
    Order,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.base import EgressEndpointDTO
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
            candidate = XrayFileProvider(runtime=None).render(
                desired, SqlAlchemyCredentialResolver(db_a)
            )  # type: ignore[arg-type]

            assert {outbound.tag for outbound in desired.outbounds} == {
                "egress-a",
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
