from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

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
from backend.app.services import (
    build_services,
    confirm_manual_payment,
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
    def start(self, request: ProvisionRequest) -> str:
        return "run-1"

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None:
        pass

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None:
        pass

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None:
        pass

    def mark_notification_failed(self, run_id: str, error: str) -> None:
        pass


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
