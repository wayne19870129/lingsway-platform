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
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    PaymentProvider,
    TenantDTO,
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

    def current_forwarder_state(self) -> DesiredForwarderState:
        return DesiredForwarderState({})

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState:
        return DesiredForwarderState({endpoint.endpoint_id: tenant.tenant_id})

    def desired_routing_state(
        self, request: ProvisionRequest, endpoint: EgressEndpointDTO, tenant: TenantDTO
    ) -> DesiredRoutingState:
        return DesiredRoutingState(
            {request.username: endpoint.endpoint_id}, (endpoint.endpoint_id,)
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
        )

    assert "order:paid" in workflow.events
    assert "subscription:provision-failed" in workflow.events
    assert accounting.disabled_users == {"customer-1"}
    assert runs.status is ProvisionStatus.FAILED
    assert "rollback" in provisioning_state.events
    assert workflow.transaction_events == ["begin", "commit", "begin", "commit"]


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
    )

    assert result is None
    assert event in workflow.events
    assert workflow.events[-1] == "subscription:active"


def test_services_compose_t3_provisioning_from_registry() -> None:
    providers = registry()
    container = build_services(State(), Runs(), providers=providers)

    assert container.providers is providers
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
