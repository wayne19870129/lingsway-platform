import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import yaml

from backend.app.domain.capacity import CapacityExceededError
from backend.app.domain.provisioning import (
    ExternalTenantCreationError,
    ProvisioningService,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionStatus,
    ProvisionStep,
)
from backend.app.domain.quota import BYTES_PER_GIB, quota_gb_to_bytes
from backend.app.domain.subscription_render import (
    CORE_RULES,
    render_routing_rules,
    render_subscription,
)
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    TenantDTO,
)
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider


@dataclass(slots=True)
class FakeState:
    events: list[str] = field(default_factory=list)
    released_endpoints: list[str] = field(default_factory=list)
    database_rolled_back: bool = False
    stored_token: str | None = None
    routing_principals_received: list[str] = field(default_factory=list)

    def mark_paid(self, order_id: str) -> None:
        self.events.append(f"paid:{order_id}")

    def reject_capacity(self, order_id: str, reason: str) -> None:
        self.events.append(f"capacity-rejected:{order_id}")

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO:
        self.events.append(f"endpoint-allocated:{customer_id}")
        return EgressEndpointDTO("egress-1", "127.0.0.1", 1080)

    def release_endpoint(self, endpoint_id: str) -> None:
        self.released_endpoints.append(endpoint_id)

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str:
        self.events.append(f"credentials:{tenant.tenant_id}:{endpoint.endpoint_id}")
        return "secret/mock-credential"

    def rollback_database(self) -> None:
        self.database_rolled_back = True

    @contextmanager
    def gateway_route_binding_lock(self) -> Iterator[None]:
        self.events.append("lock:acquire")
        try:
            yield
        finally:
            self.events.append("lock:release")

    def current_forwarder_state(self) -> DesiredForwarderState:
        return DesiredForwarderState({"existing": "outbound-existing"})

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState:
        return DesiredForwarderState({endpoint.endpoint_id: tenant.tenant_id})

    def desired_routing_state(
        self,
        request: ProvisionRequest,
        endpoint: EgressEndpointDTO,
        tenant: TenantDTO,
        routing_principal: str,
    ) -> DesiredRoutingState:
        del request
        self.routing_principals_received.append(routing_principal)
        return DesiredRoutingState(
            {routing_principal: endpoint.endpoint_id}, (endpoint.endpoint_id,)
        )

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None:
        self.stored_token = raw_token


@dataclass(slots=True)
class FakeRuns:
    steps: list[tuple[ProvisionStep, str]] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)
    status: ProvisionStatus = ProvisionStatus.RUNNING
    error: str | None = None
    notification_error: str | None = None

    def start(self, request: ProvisionRequest) -> str:
        return "run-1"

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None:
        self.steps.append((step, status))

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None:
        self.external_ids[kind] = external_id

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None:
        self.status = status
        self.error = error

    def mark_notification_failed(self, run_id: str, error: str) -> None:
        self.notification_error = error


@dataclass(slots=True)
class PartiallyCreatedEgress(MockEgressProvider):
    externally_created: list[str] = field(default_factory=list)

    def create_tenant(
        self, label: str, quota_gb: Decimal, thread_limit: int
    ) -> TenantDTO:
        external_id = "external-tenant-42"
        self.externally_created.append(external_id)
        raise ExternalTenantCreationError("provider response lost", external_id)


@dataclass(slots=True)
class TrackingAccounting(MockAccountingProvider):
    delete_calls: list[str] = field(default_factory=list)

    def delete_user(self, username: str) -> None:
        self.delete_calls.append(username)


def request() -> ProvisionRequest:
    return ProvisionRequest(
        order_id="order-1",
        customer_id="customer-1",
        username="customer-1",
        quota_gb=Decimal("50"),
        thread_limit=100,
        expire_at=datetime(2027, 1, 1, tzinfo=UTC),
        subscription_domain="subs.example.invalid",
    )


def service(
    *,
    egress: MockEgressProvider | None = None,
    accounting: MockAccountingProvider | None = None,
    state: FakeState | None = None,
    runs: FakeRuns | None = None,
) -> ProvisioningService:
    return ProvisioningService(
        egress=egress or MockEgressProvider(),
        accounting=accounting or MockAccountingProvider(),
        gateway=MockGatewayProvider(),
        forwarder=MockForwarderProvider(),
        notify=NoopNotifyProvider(),
        state=state or FakeState(),
        runs=runs or FakeRuns(),
        token_factory=lambda: "one-time-token",
    )


def test_provisioning_runs_all_nine_steps_in_order() -> None:
    runs = FakeRuns()
    outcome = service(runs=runs).provision(request())
    started_steps = [step for step, status in runs.steps if status == "STARTED"]
    assert started_steps == list(ProvisionStep)
    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert outcome.subscription_url == "https://subs.example.invalid/s/one-time-token"


def test_step_3_failure_keeps_external_tenant_and_records_pending_manual() -> None:
    egress = PartiallyCreatedEgress()
    state = FakeState()
    runs = FakeRuns()

    outcome = service(egress=egress, state=state, runs=runs).provision(request())

    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert runs.status is ProvisionStatus.PENDING_MANUAL
    assert runs.external_ids == {"egress_tenant": "external-tenant-42"}
    assert egress.externally_created == ["external-tenant-42"]
    assert state.released_endpoints == []
    assert state.database_rolled_back is False


def test_step_6_failure_disables_user_and_never_deletes() -> None:
    accounting = TrackingAccounting(failures={"create_user": RuntimeError("step 6 failed")})

    with pytest.raises(RuntimeError, match="step 6 failed"):
        service(accounting=accounting).provision(request())

    assert accounting.disabled_users == {"customer-1"}
    assert accounting.delete_calls == []


def test_capacity_failure_happens_before_mark_paid() -> None:
    egress = MockEgressProvider(total_gb=Decimal("100"), reserved_gb=Decimal("20"))
    egress.tenants["existing"] = TenantDTO("existing", "existing", Decimal("40"), 1)
    state = FakeState()

    with pytest.raises(CapacityExceededError):
        service(egress=egress, state=state).provision(request())

    assert state.events == ["capacity-rejected:order-1"]
    assert not any(event.startswith("paid:") for event in state.events)


def test_provision_prepare_runs_steps_1_to_6_without_touching_the_lock() -> None:
    """ADR-017: phase A (provision_prepare) never touches
    GatewayRouteBinding and never acquires the named lock -- the domain
    layer has never acquired this lock itself; that is the caller's job
    (see services.confirm_payment_and_provision)."""
    from backend.app.domain.provisioning import ProvisioningCheckpoint

    runs = FakeRuns()
    state = FakeState()
    checkpoint = service(state=state, runs=runs).provision_prepare(request())

    assert isinstance(checkpoint, ProvisioningCheckpoint)
    assert checkpoint.run_id == "run-1"
    assert checkpoint.endpoint.endpoint_id == "egress-1"
    assert checkpoint.tenant.tenant_id is not None
    started_steps = [step for step, status in runs.steps if status == "STARTED"]
    assert started_steps == [
        ProvisionStep.CAPACITY,
        ProvisionStep.ALLOCATE_ENDPOINT,
        ProvisionStep.CREATE_TENANT,
        ProvisionStep.STORE_CREDENTIALS,
        ProvisionStep.APPLY_FORWARDER,
        ProvisionStep.CREATE_ACCOUNTING_USER,
    ]
    assert "lock:acquire" not in state.events


def test_provision_apply_gateway_continues_from_a_checkpoint_and_uses_the_lock() -> None:
    """ADR-017: phase B is the only method that calls
    desired_routing_state(); production callers must wrap it in
    state.gateway_route_binding_lock() themselves (this test does so
    explicitly, mirroring services.confirm_payment_and_provision, since
    the domain layer itself never acquires the lock)."""
    runs = FakeRuns()
    state = FakeState()
    svc = service(state=state, runs=runs)
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)

    with state.gateway_route_binding_lock():
        outcome = svc.provision_apply_gateway(checkpoint, request())

    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert outcome.subscription_url == "https://subs.example.invalid/s/one-time-token"
    started_steps = [step for step, status in runs.steps if status == "STARTED"]
    assert started_steps == [
        ProvisionStep.CAPACITY,
        ProvisionStep.ALLOCATE_ENDPOINT,
        ProvisionStep.CREATE_TENANT,
        ProvisionStep.STORE_CREDENTIALS,
        ProvisionStep.APPLY_FORWARDER,
        ProvisionStep.CREATE_ACCOUNTING_USER,
        ProvisionStep.APPLY_GATEWAY,
        ProvisionStep.ISSUE_SUBSCRIPTION,
        ProvisionStep.NOTIFY,
    ]
    assert state.events[-2:] == ["lock:acquire", "lock:release"]


def test_provision_is_equivalent_to_prepare_then_apply_gateway() -> None:
    """provision() (kept for callers that don't need the phase split) must
    still run every step in order for a plain success case -- it is a
    composition of the two new methods, not a parallel implementation."""
    runs = FakeRuns()
    outcome = service(runs=runs).provision(request())
    started_steps = [step for step, status in runs.steps if status == "STARTED"]
    assert started_steps == list(ProvisionStep)
    assert outcome.status is ProvisionStatus.SUCCEEDED


def test_fail_apply_gateway_lock_acquisition_disables_user_and_marks_run_failed() -> None:
    """ADR-017: the compensation for GET_LOCK() itself failing must be
    identical to the in-band APPLY_GATEWAY exception handler's -- disable
    the accounting user, raise the same alert, mark the run FAILED at the
    APPLY_GATEWAY step -- since provision_apply_gateway() never got a
    chance to run its own handler."""

    accounting = TrackingAccounting()
    notify = NoopNotifyProvider()
    runs = FakeRuns()
    svc = ProvisioningService(
        egress=MockEgressProvider(),
        accounting=accounting,
        gateway=MockGatewayProvider(),
        forwarder=MockForwarderProvider(),
        notify=notify,
        state=FakeState(),
        runs=runs,
        token_factory=lambda: "one-time-token",
    )
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)

    lock_error = RuntimeError("GET_LOCK timed out")
    svc.fail_apply_gateway_lock_acquisition(checkpoint, request(), lock_error)

    assert accounting.disabled_users == {"customer-1"}
    assert accounting.delete_calls == []
    assert [event.event_type for event in notify.events] == ["GATEWAY_APPLY_FAILED"]
    assert runs.status is ProvisionStatus.FAILED
    assert runs.error == "GET_LOCK timed out"
    assert (ProvisionStep.APPLY_GATEWAY, "FAILED") in runs.steps


def test_routing_invariants_block_private_first_and_catch_all_last() -> None:
    rules = render_routing_rules({"customer-1": "egress-1"})
    assert rules[0] == {
        "type": "field",
        "ip": ["geoip:private"],
        "outboundTag": "BLOCK",
    }
    assert rules[-1] == {
        "type": "field",
        "network": "tcp,udp",
        "outboundTag": "BLOCK",
    }
    assert "DIRECT" not in str(rules).upper()


@pytest.mark.parametrize(
    ("user_agent", "expected_format"),
    [
        ("clash.meta/1.0", "CLASH"),
        ("clash meta/1.0", "CLASH"),
        ("clashmeta/1.0", "CLASH"),
        ("mihomo/1.0", "CLASH"),
        ("stash/1.0", "CLASH"),
        ("v2ray/1.0", "URI_BASE64"),
        ("v2rayN/7.0", "URI_BASE64"),
        ("v2rayng/1.0", "URI_BASE64"),
        ("sing-box/1.0", "URI_BASE64"),
        ("singbox/1.0", "URI_BASE64"),
        ("Clash_Meta/1.0", "CLASH"),
        ("curl/8.0", "CLASH"),
    ],
)
def test_production_user_agent_branches_keep_one_rule(
    user_agent: str, expected_format: str
) -> None:
    link = "vless://uuid@example.invalid:443#Tokyo"
    rendered = render_subscription([link], user_agent)

    assert rendered.client_format == expected_format
    assert rendered.rules == ("MATCH,SUBSCRIPTION",)
    if expected_format == "URI_BASE64":
        assert base64.b64decode(rendered.content).decode() == link
    else:
        document = yaml.safe_load(rendered.content)
        assert document["rules"] == ["MATCH,SUBSCRIPTION"]


def test_target_core_rules_remain_reserved_for_task_t13() -> None:
    assert len(CORE_RULES) == 13


def test_subscription_headers_are_carried_without_domain_side_effects() -> None:
    rendered = render_subscription(
        ["vless://uuid@example.invalid:443#Tokyo"],
        "clash.meta/1.0",
        subscription_userinfo="upload=0; download=12; total=100; expire=123",
        etag='"config-hash"',
    )

    assert rendered.headers == {
        "Subscription-Userinfo": "upload=0; download=12; total=100; expire=123",
        "ETag": '"config-hash"',
    }


def test_protocol_fields_and_duplicate_proxy_names_match_production() -> None:
    vmess_body = {
        "ps": "Node",
        "add": "vmess.invalid",
        "port": 443,
        "id": "vmess-uuid",
        "aid": 0,
        "scy": "auto",
        "net": "ws",
        "tls": "tls",
        "sni": "vmess.invalid",
    }
    vmess_payload = base64.urlsafe_b64encode(json.dumps(vmess_body).encode()).decode().rstrip("=")
    vless_link = (
        "vless://vless-uuid@example.invalid:443?type=ws&security=reality"
        "&sni=edge.invalid&fp=chrome&pbk=public-key&sid=short-id"
        "&flow=xtls-rprx-vision#Node"
    )

    document = yaml.safe_load(
        render_subscription(
            [vless_link, f"vmess://{vmess_payload}"], "stash/2.0"
        ).content
    )
    vless_proxy, vmess_proxy = document["proxies"]

    assert [proxy["name"] for proxy in document["proxies"]] == ["Node", "Node-2"]
    assert vless_proxy["network"] == "ws"
    assert vless_proxy["tls"] is True
    assert vless_proxy["servername"] == "edge.invalid"
    assert vless_proxy["client-fingerprint"] == "chrome"
    assert vless_proxy["flow"] == "xtls-rprx-vision"
    assert vless_proxy["reality-opts"] == {
        "public-key": "public-key",
        "short-id": "short-id",
    }
    assert vmess_proxy["network"] == "ws"
    assert vmess_proxy["tls"] is True
    assert vmess_proxy["servername"] == "vmess.invalid"


def test_quota_conversion_is_one_to_one() -> None:
    assert quota_gb_to_bytes(Decimal("50")) == 50 * BYTES_PER_GIB


def test_quota_conversion_boundary_values_are_exact_and_never_rounded() -> None:
    with pytest.raises(ValueError, match="quota must be positive"):
        quota_gb_to_bytes(Decimal("0"))

    one_byte_in_gib = Decimal(1) / Decimal(BYTES_PER_GIB)
    assert quota_gb_to_bytes(one_byte_in_gib) == 1

    with pytest.raises(ValueError, match="whole number of bytes"):
        quota_gb_to_bytes(Decimal("0.0000000005"))
