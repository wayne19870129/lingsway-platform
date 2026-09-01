"""Application service composition.

Concrete integrations are selected once by ``providers.registry``.  This module
only composes those contracts with domain services; it does not instantiate a
provider implementation or contain an alternate provisioning sequence.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.core.config import Settings, get_settings
from backend.app.domain.provisioning import (
    ProvisioningService,
    ProvisioningState,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionRunStore,
)
from backend.app.domain.subscription_render import RenderedSubscription, render_subscription
from backend.app.providers.registry import ProviderRegistry, build_registry


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    """All application services sharing one registry-selected provider set."""

    providers: ProviderRegistry
    provisioning: ProvisioningService


def build_services(
    state: ProvisioningState,
    runs: ProvisionRunStore,
    *,
    settings: Settings | None = None,
    providers: ProviderRegistry | None = None,
) -> ServiceContainer:
    """Compose domain services from the environment-selected provider registry."""
    registry = providers or build_registry(settings or get_settings())
    return ServiceContainer(
        providers=registry,
        provisioning=ProvisioningService(
            egress=registry.egress,
            accounting=registry.accounting,
            gateway=registry.gateway,
            forwarder=registry.forwarder,
            notify=registry.notify,
            state=state,
            runs=runs,
        ),
    )


def provision(
    request: ProvisionRequest,
    state: ProvisioningState,
    runs: ProvisionRunStore,
    *,
    settings: Settings | None = None,
    providers: ProviderRegistry | None = None,
) -> ProvisionOutcome:
    """Run the single supported T3 nine-step provisioning orchestration."""
    return build_services(
        state, runs, settings=settings, providers=providers
    ).provisioning.provision(request)


def confirm_manual_payment(
    order: object,
    reference: str,
    *,
    settings: Settings | None = None,
    providers: ProviderRegistry | None = None,
) -> None:
    """Forward manual payment confirmation through the selected provider."""
    registry = providers or build_registry(settings or get_settings())
    registry.payment.confirm_manual(order, reference)


def render_customer_subscription(
    links: list[str],
    user_agent: str,
) -> RenderedSubscription:
    """Keep subscription rendering in the domain renderer."""
    return render_subscription(links, user_agent)
