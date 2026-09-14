from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.domain.ordering import (
    BillingCommand,
    BillingOrderType,
    PaymentConfirmation,
)
from backend.app.domain.provisioning import (
    ProvisionRequest,
    ProvisionStatus,
    ProvisionStep,
)
from backend.app.domain.subscription_render import RenderedSubscription
from backend.app.infra.gateway_route_lock import GatewayRouteBindingLockError
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    PaymentProvider,
    TenantDTO,
    XrayOutboundDTO,
)
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.registry import ProviderRegistry
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider
from backend.app.services import (
    build_services,
    confirm_manual_payment,
    confirm_payment_and_provision,
    render_customer_subscription,
)


@dataclass
class State:
    events: list[str] = field(default_factory=list)
    lock_acquire_count: int = 0
    lock_error: Exception | None = None

    def mark_paid(self, order_id: str) -> None:
        self.events.append(f"paid:{order_id}")

    def reject_capacity(self, order_id: str, reason: str) -> None:
        self.events.append(f"rejected:{order_id}")

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO:
        return EgressEndpointDTO("egress-1", "127.0.0.1", 1080)

    def release_endpoint(self, endpoint_id: str) -> None:
        self.events.append(f"released:{endpoint_id}")

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str:
        return "secret/mock"

    def rollback_database(self) -> None:
        self.events.append("rollback")

    @contextmanager
    def gateway_route_binding_lock(self) -> Iterator[None]:
        self.lock_acquire_count += 1
        if self.lock_error is not None:
            # ADR-017: simulates GET_LOCK() itself failing -- __enter__
            # raises before the `with` block body (the caller's try/except
            # around provision_apply_gateway) ever runs.
            raise self.lock_error
        self.events.append("lock:acquire")
        try:
            yield
        finally:
            self.events.append("lock:release")

    def current_forwarder_state(self) -> DesiredForwarderState:
        return DesiredForwarderState({})

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
        self.events.append(f"desired-routing-state:{routing_principal}")
        return DesiredRoutingState(
            user_routes={routing_principal: endpoint.endpoint_id},
            outbounds=(
                XrayOutboundDTO(
                    endpoint.endpoint_id,
                    endpoint.host,
                    endpoint.port,
                    endpoint.protocol,
                    "secret/mock",
                ),
            ),
        )

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None:
        self.events.append("token")


@dataclass
class Runs:
    status: ProvisionStatus | None = None

    def start(self, request: ProvisionRequest) -> str:
        return "run-1"

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None:
        pass

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None:
        pass

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None:
        self.status = status

    def mark_notification_failed(self, run_id: str, error: str) -> None:
        pass


@dataclass(frozen=True)
class FakeCredentialResolver:
    def resolve(self, secret_ref: str) -> CredentialDTO:
        return CredentialDTO("resolver-user", "resolver-password")


@dataclass
class WorkflowState:
    events: list[str] = field(default_factory=list)
    transaction_events: list[str] = field(default_factory=list)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transaction_events.append("begin")
        try:
            yield
        except Exception:
            self.transaction_events.append("rollback")
            raise
        else:
            self.transaction_events.append("commit")

    def record_payment(self, order_id: str, receipt: PaymentConfirmation) -> None:
        self.events.append(f"paid:{order_id}:{receipt.external_reference}")

    def reject_capacity(self, order_id: str, reason: str) -> None:
        self.events.append(f"capacity-rejected:{order_id}")

    def prepare_purchase(self, command: BillingCommand) -> None:
        self.events.append("subscription:provisioning")

    def apply_renewal(self, command: BillingCommand) -> None:
        self.events.append("billing:renewal")

    def apply_upgrade(self, command: BillingCommand) -> None:
        self.events.append("billing:upgrade")

    def apply_addon(self, command: BillingCommand) -> None:
        self.events.append("billing:addon")

    def activate_subscription(self, command: BillingCommand) -> None:
        self.events.append("subscription:active")

    def mark_provision_pending(self, command: BillingCommand, error: str) -> None:
        self.events.append("provision:pending-manual")

    def mark_provision_failed(self, command: BillingCommand, error: str) -> None:
        self.events.extend(["order:paid", "subscription:provision-failed"])


def registry(payment: PaymentProvider | None = None) -> ProviderRegistry:
    return ProviderRegistry(
        egress=MockEgressProvider(),
        accounting=MockAccountingProvider(),
        gateway=MockGatewayProvider(),
        forwarder=MockForwarderProvider(),
        payment=payment or MockPaymentProvider(),
        notify=NoopNotifyProvider(),
        email=NoopEmailProvider(),
        captcha=NoopCaptchaProvider(),
        storage=MockBlobStorage(),
        transport=MockTransportProvider(),
    )


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


def command(order_type: BillingOrderType = BillingOrderType.PURCHASE) -> BillingCommand:
    return BillingCommand(
        order_id="order-1",
        subscription_id="subscription-1" if order_type is not BillingOrderType.PURCHASE else None,
        order_type=order_type,
        addon_bytes=10 if order_type is BillingOrderType.ADDON else 0,
    )


def test_external_failure_keeps_paid_order_marks_subscription_failed_and_disables_user() -> None:
    providers = registry()
    credential_resolver = FakeCredentialResolver()
    accounting = MockAccountingProvider(
        failures={"create_user": RuntimeError("accounting unavailable")}
    )
    providers = ProviderRegistry(
        egress=providers.egress,
        accounting=accounting,
        gateway=providers.gateway,
        forwarder=providers.forwarder,
        payment=providers.payment,
        notify=providers.notify,
        email=providers.email,
        captcha=providers.captcha,
        storage=providers.storage,
        transport=providers.transport,
    )
    workflow = WorkflowState()
    runs = Runs()
    provisioning_state = State()

    with pytest.raises(RuntimeError, match="accounting unavailable"):
        confirm_payment_and_provision(
            command(),
            request(),
            object(),
            provisioning_state,
            runs,
            workflow,
            "receipt-1",
            providers=providers,
            credential_resolver=credential_resolver,
        )

    assert "order:paid" in workflow.events
    assert "subscription:provision-failed" in workflow.events
    assert accounting.disabled_users == {"customer-1"}
    assert runs.status is ProvisionStatus.FAILED
    assert "rollback" in provisioning_state.events
    assert workflow.transaction_events == ["begin", "commit", "begin", "commit"]
    # ADR-017: CREATE_ACCOUNTING_USER (step 6) is part of phase A
    # (provision_prepare), which runs before the named lock is ever
    # touched -- this failure must never reach it.
    assert provisioning_state.lock_acquire_count == 0


def test_pending_manual_never_acquires_the_named_lock() -> None:
    """ADR-017: CREATE_TENANT -> PENDING_MANUAL is also a phase-A-only
    outcome; it must return before the named lock is ever touched."""
    from backend.app.providers.egress.mock import MockEgressProvider

    class PartiallyCreatedEgress(MockEgressProvider):
        def create_tenant(self, label, quota_gb, thread_limit):  # type: ignore[no-untyped-def]
            from backend.app.domain.provisioning import ExternalTenantCreationError

            raise ExternalTenantCreationError("provider response lost", "external-tenant-1")

    providers = registry()
    credential_resolver = FakeCredentialResolver()
    providers = ProviderRegistry(
        egress=PartiallyCreatedEgress(),
        accounting=providers.accounting,
        gateway=providers.gateway,
        forwarder=providers.forwarder,
        payment=providers.payment,
        notify=providers.notify,
        email=providers.email,
        captcha=providers.captcha,
        storage=providers.storage,
        transport=providers.transport,
    )
    workflow = WorkflowState()
    provisioning_state = State()

    outcome = confirm_payment_and_provision(
        command(),
        request(),
        object(),
        provisioning_state,
        Runs(),
        workflow,
        "receipt-1",
        providers=providers,
        credential_resolver=credential_resolver,
    )

    assert outcome is not None
    assert outcome.status is ProvisionStatus.PENDING_MANUAL
    assert "provision:pending-manual" in workflow.events
    assert provisioning_state.lock_acquire_count == 0
    assert "rollback" not in provisioning_state.events


def test_lock_acquisition_failure_disables_accounting_user_and_rolls_back() -> None:
    """ADR-017: steps 1-6 already ran (accounting user created) before
    GET_LOCK() is attempted. If it fails, provision_apply_gateway() never
    runs -- so its own APPLY_GATEWAY exception handler (disable user,
    alert, mark run failed) never fires either. This must still happen,
    via the dedicated lock-acquisition-failure compensation path, plus the
    same DB rollback and terminal order state any other mid-saga failure
    gets. Critically: zero GatewayRouteBinding mutation, since
    provision_apply_gateway() (the only caller of desired_routing_state())
    never runs at all."""
    providers = registry()
    credential_resolver = FakeCredentialResolver()
    workflow = WorkflowState()
    runs = Runs()
    provisioning_state = State(lock_error=GatewayRouteBindingLockError("GET_LOCK timed out"))

    with pytest.raises(GatewayRouteBindingLockError, match="GET_LOCK timed out"):
        confirm_payment_and_provision(
            command(),
            request(),
            object(),
            provisioning_state,
            runs,
            workflow,
            "receipt-1",
            providers=providers,
            credential_resolver=credential_resolver,
        )

    assert isinstance(providers.accounting, MockAccountingProvider)
    assert providers.accounting.disabled_users == {"customer-1"}
    assert runs.status is ProvisionStatus.FAILED
    assert "rollback" in provisioning_state.events
    assert "order:paid" in workflow.events
    assert "subscription:provision-failed" in workflow.events
    # desired_routing_state()/ensure_gateway_route_binding() must never
    # have been reached -- "lock:acquire" is only appended on a
    # successful (non-raising) lock acquisition in this fake.
    assert "lock:acquire" not in provisioning_state.events
    assert provisioning_state.lock_acquire_count == 1


@pytest.mark.parametrize(
    ("order_type", "event"),
    [
        (BillingOrderType.RENEWAL, "billing:renewal"),
        (BillingOrderType.UPGRADE, "billing:upgrade"),
        (BillingOrderType.ADDON, "billing:addon"),
    ],
)
def test_paid_non_purchase_orders_use_ordering_branch(
    order_type: BillingOrderType, event: str
) -> None:
    providers = registry()
    credential_resolver = FakeCredentialResolver()
    workflow = WorkflowState()

    result = confirm_payment_and_provision(
        command(order_type),
        request(),
        object(),
        State(),
        Runs(),
        workflow,
        "receipt-1",
        providers=providers,
        credential_resolver=credential_resolver,
    )

    assert result is None
    assert event in workflow.events
    assert workflow.events[-1] == "subscription:active"


def test_services_compose_t3_provisioning_from_registry() -> None:
    providers = registry()
    credential_resolver = FakeCredentialResolver()
    container = build_services(
        State(), Runs(), providers=providers, credential_resolver=credential_resolver
    )

    assert container.providers is providers
    assert container.provisioning.credential_resolver is credential_resolver
    outcome = container.provisioning.provision(request())
    assert outcome.status is ProvisionStatus.SUCCEEDED


def test_manual_payment_is_forwarded_to_registry_provider() -> None:
    payment = MockPaymentProvider()
    confirm_manual_payment(object(), "reference-1", providers=registry(payment))

    assert payment.confirmed_references == ["reference-1"]


def test_subscription_rendering_remains_domain_owned() -> None:
    rendered = render_customer_subscription(
        ["vless://uuid@example.invalid:443#test"], "v2rayN/7.0"
    )

    assert isinstance(rendered, RenderedSubscription)
    assert rendered.client_format == "URI_BASE64"
