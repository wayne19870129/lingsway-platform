"""Application service composition.

Concrete integrations are selected once by ``providers.registry``.  This module
only composes those contracts with domain services; it does not instantiate a
provider implementation or contain an alternate provisioning sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from backend.app.core.config import Settings, get_settings
from backend.app.domain.capacity import ensure_capacity
from backend.app.domain.ordering import (
    BillingCommand,
    BillingOrderType,
    OrderWorkflowState,
    PaymentConfirmation,
    activate_paid_purchase,
    apply_paid_billing_change,
    fail_paid_purchase,
)
from backend.app.domain.provisioning import (
    ProvisioningService,
    ProvisioningState,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionRunStore,
    ProvisionStatus,
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


def confirm_payment_and_provision(
    command: BillingCommand,
    request: ProvisionRequest,
    order: object,
    state: ProvisioningState,
    runs: ProvisionRunStore,
    order_state: OrderWorkflowState,
    payment_reference: str,
    *,
    settings: Settings | None = None,
    providers: ProviderRegistry | None = None,
    payment_provider_code: str = "manual",
) -> ProvisionOutcome | None:
    """Run the three-phase paid-order workflow.

    Ordering owns DB transitions, while ``ProvisioningService`` owns only the
    external nine-step saga.  Each DB phase has its own transaction supplied by
    ``order_state``; no provider call is made while one is open.
    """
    registry = providers or build_registry(settings or get_settings())
    capacity = registry.egress.capacity()

    # Capacity is checked before the payment provider is called or PAID is
    # persisted.  It is an external read, not a DB state transition.
    try:
        ensure_capacity(capacity, request.quota_gb)
    except Exception as exc:
        with order_state.transaction():
            order_state.reject_capacity(command.order_id, str(exc))
        raise

    registry.payment.confirm_manual(order, payment_reference)
    receipt = PaymentConfirmation(
        provider_code=payment_provider_code,
        external_reference=payment_reference,
        paid_at=datetime.now(UTC),
    )
    with order_state.transaction():
        order_state.record_payment(command.order_id, receipt)
        if command.order_type is BillingOrderType.PURCHASE:
            order_state.prepare_purchase(command)
        else:
            apply_paid_billing_change(command, order_state)
            order_state.activate_subscription(command)
            return None

    try:
        outcome = build_services(
            state, runs, settings=settings, providers=registry
        ).provisioning.provision(request)
        if outcome.status is ProvisionStatus.PENDING_MANUAL:
            with order_state.transaction():
                order_state.mark_provision_pending(
                    command, "external tenant creation requires review"
                )
            return outcome
    except Exception as exc:
        # The provider-specific saga performs its external compensation.  The
        # DB terminal state is written only after that compensation returns.
        state.rollback_database()
        with order_state.transaction():
            fail_paid_purchase(command, type(exc).__name__, order_state)
        raise

    with order_state.transaction():
        activate_paid_purchase(command, order_state)
    return outcome


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
