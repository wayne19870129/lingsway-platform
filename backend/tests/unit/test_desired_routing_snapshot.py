from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from backend.app.infra.provisioning_state import (
    DesiredRoutingStateError,
    SqlAlchemyProvisioningState,
)
from backend.app.models import EgressBinding, EgressEndpoint, GatewayRouteBinding
from backend.app.providers.base import XrayOutboundDTO


class _Result:
    def __init__(self, rows: list[tuple[Any, Any, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, Any, Any]]:
        return self._rows


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _SnapshotSession:
    def __init__(self, rows: list[tuple[Any, Any, Any]], bindings: list[EgressBinding]) -> None:
        self.info: dict[str, object] = {}
        self.statements: list[Any] = []
        self._rows = rows
        self._bindings = bindings

    def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self._rows)

    def scalars(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self._bindings)


def _endpoint(endpoint_id: int, ref: str = "endpoint/ref") -> EgressEndpoint:
    return EgressEndpoint(
        id=endpoint_id,
        group_id=1,
        code=f"endpoint-{endpoint_id}",
        provider_name="webshare",
        host=f"proxy-{endpoint_id}.example.invalid",
        port=1080 + endpoint_id,
        protocol="socks5",
        credential_secret_ref=ref,
        mihomo_listen_port=11080 + endpoint_id,
    )


def _route(
    route_id: int,
    subscription_id: int,
    endpoint_id: int,
    principal: str,
    tag: str,
) -> GatewayRouteBinding:
    return GatewayRouteBinding(
        id=route_id,
        subscription_id=subscription_id,
        egress_id=endpoint_id,
        gateway_principal=principal,
        outbound_tag=tag,
        enabled=True,
    )


def test_full_snapshot_is_complete_deterministic_and_uses_override_precedence() -> None:
    endpoint_a = _endpoint(1)
    endpoint_b = _endpoint(2)
    route_a = _route(1, 11, 1, "principal-a", "egress-a")
    route_b = _route(2, 22, 2, "principal-b", "egress-b")
    binding_b = EgressBinding(
        id=1,
        subscription_id=22,
        egress_id=2,
        credential_secret_ref="binding/ref",
    )
    db = _SnapshotSession(
        [(route_b, endpoint_b, binding_b), (route_a, endpoint_a, None)],
        [binding_b],
    )

    desired = SqlAlchemyProvisioningState(cast(Session, db), 11)._full_desired_routing_snapshot()

    assert desired.user_routes == {
        "principal-a": "egress-a",
        "principal-b": "egress-b",
    }
    assert desired.outbounds == (
        XrayOutboundDTO("egress-a", endpoint_a.host, endpoint_a.port, "socks5", "endpoint/ref"),
        XrayOutboundDTO("egress-b", endpoint_b.host, endpoint_b.port, "socks5", "binding/ref"),
    )
    assert all(
        statement.get_execution_options().get("populate_existing") is True
        for statement in db.statements
    )
    assert "FOR UPDATE" in str(db.statements[0].compile(dialect=mysql.dialect()))


@pytest.mark.parametrize(
    ("rows", "bindings", "code"),
    [
        (
            [(_route(1, 1, 1, "same", "egress-a"), _endpoint(1), None),
             (_route(2, 2, 2, "same", "egress-b"), _endpoint(2), None)],
            [],
            "ROUTE_PRINCIPAL_DUPLICATE",
        ),
        ([( _route(1, 1, 1, "principal", "egress-a"), None, None)], [], "ROUTE_ENDPOINT_MISSING"),
        (
            [(_route(1, 1, 1, "principal", "egress-a"), _endpoint(1), None)],
            [EgressBinding(subscription_id=1, egress_id=2, credential_secret_ref="other/ref")],
            "ROUTE_BINDING_EGRESS_MISMATCH",
        ),
    ],
)
def test_full_snapshot_rejects_invalid_active_rows(
    rows: list[tuple[Any, Any, Any]], bindings: list[EgressBinding], code: str
) -> None:
    db = _SnapshotSession(rows, bindings)

    with pytest.raises(DesiredRoutingStateError) as raised:
        SqlAlchemyProvisioningState(cast(Session, db), 1)._full_desired_routing_snapshot()

    assert raised.value.code == code
    assert str(raised.value) == code


def test_nonempty_whitespace_override_is_selected_without_endpoint_fallback() -> None:
    endpoint = _endpoint(1, "endpoint/ref")
    route = _route(1, 1, 1, "principal", "egress-a")
    override = EgressBinding(
        subscription_id=1,
        egress_id=1,
        credential_secret_ref="   ",
    )
    db = _SnapshotSession([(route, endpoint, override)], [override])

    desired = SqlAlchemyProvisioningState(cast(Session, db), 1)._full_desired_routing_snapshot()

    assert desired.outbounds[0].credential_secret_ref == "   "
