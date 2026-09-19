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
from backend.app.domain.ordering import BillingCommand, BillingOrderType, apply_paid_billing_change
from backend.app.domain.provisioning import (
    PENDING_MANUAL_BUSINESS_MESSAGES,
    ExternalTenantCreationError,
    PendingManualReason,
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
    AccountingCreateEffect,
    AccountingCreateUserError,
    AccountUserDTO,
    ApplyResult,
    CandidateConfig,
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    TenantDTO,
    XrayOutboundDTO,
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
            user_routes={routing_principal: endpoint.endpoint_id},
            outbounds=(
                XrayOutboundDTO(
                    endpoint.endpoint_id,
                    endpoint.host,
                    endpoint.port,
                    endpoint.protocol,
                    "secret/mock-credential",
                ),
            ),
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


@dataclass(frozen=True, slots=True)
class FakeCredentialResolver:
    def resolve(self, secret_ref: str) -> CredentialDTO:
        return CredentialDTO(username="resolver-user", password="resolver-password")


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
    credential_resolver: FakeCredentialResolver | None = None,
    gateway: MockGatewayProvider | None = None,
) -> ProvisioningService:
    return ProvisioningService(
        egress=egress or MockEgressProvider(),
        accounting=accounting or MockAccountingProvider(),
        gateway=gateway or MockGatewayProvider(),
        credential_resolver=credential_resolver or FakeCredentialResolver(),
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


def test_provisioning_passes_exact_resolver_to_gateway() -> None:
    class RecordingGateway(MockGatewayProvider):
        received_resolver: object | None = None

        def render(self, desired, resolver):  # type: ignore[no-untyped-def]
            self.received_resolver = resolver
            return super().render(desired, resolver)

    resolver = FakeCredentialResolver()
    gateway = RecordingGateway()

    outcome = service(gateway=gateway, credential_resolver=resolver).provision(request())

    assert outcome.status is ProvisionStatus.SUCCEEDED
    assert gateway.received_resolver is resolver


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


@dataclass(slots=True)
class _NoSideEffectAccounting(TrackingAccounting):
    """ADR-018 round 2 (Major 1): simulates a pre-dispatch
    request-construction failure (e.g. a naive/pre-epoch expire_at) --
    the kind of failure that never reaches the network, and must
    therefore be classified NO_SIDE_EFFECT, never treated as CREATED."""

    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        raise AccountingCreateUserError(
            "simulated pre-dispatch validation failure",
            effect=AccountingCreateEffect.NO_SIDE_EFFECT,
        )


def test_step_6_no_side_effect_never_disables() -> None:
    accounting = _NoSideEffectAccounting()
    state = FakeState()
    runs = FakeRuns()

    with pytest.raises(AccountingCreateUserError, match="pre-dispatch"):
        service(accounting=accounting, state=state, runs=runs).provision(request())

    assert accounting.disabled_users == set()
    assert accounting.delete_calls == []
    assert state.database_rolled_back is True
    assert runs.status is ProvisionStatus.FAILED


@dataclass(slots=True)
class _AmbiguousAccounting(TrackingAccounting):
    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        raise AccountingCreateUserError(
            "simulated transport failure after dispatch",
            effect=AccountingCreateEffect.AMBIGUOUS,
        )


def test_step_6_ambiguous_never_disables_and_uses_correct_pending_reason() -> None:
    accounting = _AmbiguousAccounting()
    state = FakeState()
    runs = FakeRuns()

    outcome = service(accounting=accounting, state=state, runs=runs).provision(request())

    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.reason is PendingManualReason.ACCOUNTING_CREATE_AMBIGUOUS
    assert accounting.disabled_users == set()
    assert accounting.delete_calls == []
    assert "lock:acquire" not in state.events


@dataclass(slots=True)
class _CreatedDisableFailsAccounting(TrackingAccounting):
    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        raise AccountingCreateUserError(
            "simulated postcondition failure",
            effect=AccountingCreateEffect.CREATED,
        )

    def disable_user(self, username: str) -> None:
        raise RuntimeError("simulated disable failure")


def test_step_6_created_disable_failure_uses_correct_pending_reason() -> None:
    accounting = _CreatedDisableFailsAccounting()
    state = FakeState()
    runs = FakeRuns()

    outcome = service(accounting=accounting, state=state, runs=runs).provision(request())

    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.reason is PendingManualReason.ACCOUNTING_COMPENSATION_FAILED
    assert outcome.pending_manual_error is not None
    assert "AccountingCreateUserError" in outcome.pending_manual_error
    assert "RuntimeError" in outcome.pending_manual_error
    assert accounting.delete_calls == []


# --- ADR-026: compensation-action failures ---------------------------------
#
# Before ADR-026 all four cases below shared one defect: the compensating
# action was a bare call, so when it raised, the compensation exception
# replaced the original failure, state.rollback_database() and the terminal
# run status were both skipped, and the run stayed at RUNNING forever while a
# real external side effect (unrestored forwarder config / still-enabled
# accounting user) went unclaimed.


@dataclass(slots=True)
class _ForwarderFailsOnApply(MockForwarderProvider):
    """First apply() is the real failure; later applies are the restore."""

    applies: int = 0
    restore_raises: bool = False

    def render(self, desired: DesiredForwarderState) -> CandidateConfig:
        return CandidateConfig({"listeners": dict(desired.listeners)}, "v1")

    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        self.applies += 1
        if self.applies == 1:
            raise RuntimeError("simulated forwarder apply failure")
        if self.restore_raises:
            raise OSError("simulated forwarder restore failure")
        return ApplyResult(applied=True, version=candidate.version)


def _service_with_forwarder(
    forwarder: MockForwarderProvider, state: FakeState, runs: FakeRuns
) -> ProvisioningService:
    return ProvisioningService(
        egress=MockEgressProvider(),
        accounting=MockAccountingProvider(),
        gateway=MockGatewayProvider(),
        credential_resolver=FakeCredentialResolver(),
        forwarder=forwarder,
        notify=NoopNotifyProvider(),
        state=state,
        runs=runs,
        token_factory=lambda: "one-time-token",
    )


def test_step_5_failure_with_successful_restore_stays_failed() -> None:
    """Regression guard: when the compensating restore succeeds, behavior is
    unchanged -- the ORIGINAL exception propagates, the DB is rolled back,
    and the run is durably FAILED."""
    forwarder = _ForwarderFailsOnApply(restore_raises=False)
    state, runs = FakeState(), FakeRuns()

    with pytest.raises(RuntimeError, match="simulated forwarder apply failure"):
        _service_with_forwarder(forwarder, state, runs).provision_prepare(request())

    assert forwarder.applies == 2, "the previous configuration must be restored"
    assert state.database_rolled_back is True
    assert runs.status is ProvisionStatus.FAILED


def test_step_5_failure_with_failing_restore_is_pending_manual() -> None:
    """ADR-026: the restore itself failed, so forwarder runtime state is
    unknown. The original failure must not be masked, and this must never be
    reported as a fully-compensated FAILED."""
    forwarder = _ForwarderFailsOnApply(restore_raises=True)
    state, runs = FakeState(), FakeRuns()

    outcome = _service_with_forwarder(forwarder, state, runs).provision_prepare(request())

    assert isinstance(outcome, ProvisionOutcome)
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.reason is PendingManualReason.FORWARDER_COMPENSATION_FAILED
    assert outcome.pending_manual_error is not None
    # Both the original failure and the restore failure are identified, by
    # exception TYPE only -- never raw provider text.
    assert "RuntimeError" in outcome.pending_manual_error
    assert "OSError" in outcome.pending_manual_error
    assert "simulated" not in outcome.pending_manual_error
    # PENDING_MANUAL preserves partial state for review and never writes the
    # terminal status itself (ADR-017) -- the caller does, after its commit.
    assert state.database_rolled_back is False
    assert runs.status is ProvisionStatus.RUNNING
    assert (ProvisionStep.APPLY_FORWARDER, "PENDING_MANUAL") in runs.steps


@dataclass(slots=True)
class _GatewayFailsOnApply(MockGatewayProvider):
    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        raise RuntimeError("simulated gateway apply failure")


@dataclass(slots=True)
class _DisableFailsAccounting(TrackingAccounting):
    def disable_user(self, username: str) -> None:
        raise OSError("simulated disable failure")


def test_step_7_failure_with_successful_disable_stays_failed() -> None:
    """Regression guard for the compensated APPLY_GATEWAY path."""
    accounting = TrackingAccounting()
    state, runs = FakeState(), FakeRuns()
    svc = service(
        accounting=accounting, gateway=_GatewayFailsOnApply(), state=state, runs=runs
    )
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)
    state.database_rolled_back = False

    with pytest.raises(RuntimeError, match="simulated gateway apply failure"):
        svc.provision_apply_gateway(checkpoint, request())

    assert "customer-1" in accounting.disabled_users
    assert accounting.delete_calls == [], "AGENTS.md 铁律 4: disable, never DELETE"
    assert state.database_rolled_back is True
    assert runs.status is ProvisionStatus.FAILED


def test_step_7_failure_with_failing_disable_is_pending_manual() -> None:
    """ADR-026: the accounting user may still be enabled, so this must be
    PENDING_MANUAL rather than a FAILED that claims full compensation."""
    accounting = _DisableFailsAccounting()
    state, runs = FakeState(), FakeRuns()
    svc = service(
        accounting=accounting, gateway=_GatewayFailsOnApply(), state=state, runs=runs
    )
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)
    state.database_rolled_back = False

    outcome = svc.provision_apply_gateway(checkpoint, request())

    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert outcome.reason is PendingManualReason.GATEWAY_COMPENSATION_FAILED
    assert outcome.pending_manual_error is not None
    assert "RuntimeError" in outcome.pending_manual_error
    assert "OSError" in outcome.pending_manual_error
    assert accounting.delete_calls == []
    assert state.database_rolled_back is False
    assert runs.status is ProvisionStatus.RUNNING
    assert (ProvisionStep.APPLY_GATEWAY, "PENDING_MANUAL") in runs.steps


def test_provision_never_marks_a_pending_manual_phase_b_run_succeeded() -> None:
    """ADR-026: provision()'s composition must branch on the new phase-B
    PENDING_MANUAL outcome -- marking it SUCCEEDED would durably claim a
    provisioning that never applied its gateway configuration."""
    runs = FakeRuns()
    outcome = service(
        accounting=_DisableFailsAccounting(), gateway=_GatewayFailsOnApply(), runs=runs
    ).provision(request())

    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert runs.status is ProvisionStatus.PENDING_MANUAL


def test_lock_acquisition_compensation_failure_is_pending_manual() -> None:
    """ADR-017 requires the lock-acquisition failure path and APPLY_GATEWAY's
    own handler not to drift apart semantically; ADR-026 extends that to the
    compensation-failed case."""
    accounting = _DisableFailsAccounting()
    runs = FakeRuns()
    svc = service(accounting=accounting, runs=runs)
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)

    pending = svc.fail_apply_gateway_lock_acquisition(
        checkpoint, request(), RuntimeError("simulated lock acquisition failure")
    )

    assert pending is not None
    assert pending.status is ProvisionStatus.PENDING_MANUAL
    assert pending.reason is PendingManualReason.GATEWAY_COMPENSATION_FAILED
    assert accounting.delete_calls == []
    assert runs.status is ProvisionStatus.RUNNING


def test_lock_acquisition_compensation_success_returns_none_and_marks_failed() -> None:
    accounting = TrackingAccounting()
    runs = FakeRuns()
    svc = service(accounting=accounting, runs=runs)
    checkpoint = svc.provision_prepare(request())
    assert not isinstance(checkpoint, ProvisionOutcome)

    assert (
        svc.fail_apply_gateway_lock_acquisition(
            checkpoint, request(), RuntimeError("simulated lock acquisition failure")
        )
        is None
    )
    assert "customer-1" in accounting.disabled_users
    assert runs.status is ProvisionStatus.FAILED


def test_every_pending_manual_reason_has_a_business_message() -> None:
    """ADR-018 Major 2: services.py looks the business-facing
    Subscription.provision_error message up by reason. A reason with no entry
    would KeyError at exactly the moment a run needs manual review."""
    for reason in PendingManualReason:
        assert reason in PENDING_MANUAL_BUSINESS_MESSAGES
        message = PENDING_MANUAL_BUSINESS_MESSAGES[reason]
        assert message and message == message.strip()


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
        credential_resolver=FakeCredentialResolver(),
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


@pytest.mark.parametrize("order_type", [BillingOrderType.UPGRADE, BillingOrderType.ADDON])
def test_paid_billing_change_rejects_upgrade_and_addon(order_type: BillingOrderType) -> None:
    class State:
        def apply_upgrade(self, command: BillingCommand) -> None:
            raise AssertionError("upgrade must fail closed")

        def apply_addon(self, command: BillingCommand) -> None:
            raise AssertionError("addon must fail closed")

    command = BillingCommand("1", "1", order_type, plan_id="1", addon_bytes=1)
    with pytest.raises(ValueError, match=order_type.value):
        apply_paid_billing_change(command, State())  # type: ignore[arg-type]
