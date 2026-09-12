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
)
from backend.app.domain.subscription_render import RenderedSubscription, render_subscription
from backend.app.infra.gateway_route_lock import GatewayRouteBindingLockError
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

    # ADR-017: steps 1-6 (CAPACITY..CREATE_ACCOUNTING_USER) never touch
    # GatewayRouteBinding, so they run unlocked. Only phase B
    # (provision_apply_gateway, APPLY_GATEWAY..NOTIFY) may mutate it, so
    # the named lock is acquired only immediately before that call --
    # never for the external-provider-only steps below.
    provisioning = build_services(state, runs, settings=settings, providers=registry).provisioning
    try:
        prepared = provisioning.provision_prepare(request)
    except Exception as exc:
        # Steps 1-6 run without the named lock (ADR-017) but still share
        # the one DB transaction/session with phase B: any failure here
        # (capacity/allocate/credentials/forwarder/accounting-create) must
        # roll it back, same as a phase-B failure does. Calling this after
        # STORE_CREDENTIALS' own internal rollback_database() call is a
        # harmless no-op, not a double-rollback bug.
        state.rollback_database()
        with order_state.transaction():
            fail_paid_purchase(command, type(exc).__name__, order_state)
        raise

    if isinstance(prepared, ProvisionOutcome):
        # PENDING_MANUAL: terminal, and reached without ever touching the
        # named lock -- do not proceed to phase B.
        with order_state.transaction():
            order_state.mark_provision_pending(
                command, "external tenant creation requires review"
            )
        return prepared

    # ADR-017/ADR-016: desired_routing_state() (invoked from
    # provision_apply_gateway()'s APPLY_GATEWAY step) mutates
    # GatewayRouteBinding but only flushes -- the actual commit
    # (activate_paid_purchase, below) or rollback
    # (state.rollback_database(), in the except branches) happens later,
    # in this same function. The named lock must therefore be held across
    # this entire span, not just around the mutation itself: releasing it
    # any earlier would let a concurrent writer observe this transaction's
    # uncommitted GatewayRouteBinding state as if it were free to proceed.
    try:
        with state.gateway_route_binding_lock():
            try:
                outcome = provisioning.provision_apply_gateway(prepared, request)
            except Exception:
                # The provider-specific saga performs its external
                # compensation. The DB terminal state is written only after
                # that compensation returns. This rollback must complete
                # before the lock (held since just above) is released, so
                # it stays inside this `with` block.
                state.rollback_database()
                raise

            with order_state.transaction():
                activate_paid_purchase(command, order_state)
            # ADR-017 run-persistence-ordering revision: the run's terminal
            # SUCCEEDED status is only ever recorded here, after
            # activate_paid_purchase()'s own commit above has actually
            # completed -- never inside provision_apply_gateway() itself.
            # mark_apply_gateway_succeeded() is best-effort (never raises),
            # so a Job-write hiccup at this point cannot turn this
            # already-committed business success into a reported failure.
            provisioning.mark_apply_gateway_succeeded(prepared.run_id)
    except GatewayRouteBindingLockError as lock_exc:
        # ADR-017: GET_LOCK() itself failed (now including any acquisition-
        # stage DBAPI/SQLAlchemy exception, normalized into this same type
        # by gateway_route_binding_write() -- see its docstring) --
        # provision_apply_gateway() never ran, so its own APPLY_GATEWAY
        # exception handler (disable accounting user, alert, mark run
        # FAILED) never ran either. Steps 1-6 already flushed DB state and
        # made real external calls (tenant, forwarder, accounting user),
        # which still need the same compensation and rollback any other
        # mid-saga failure gets.
        #
        # Ordering matters here even though runs.mark_status() persists on
        # its own dedicated Session (see _SqlAlchemyProvisionRuns), never
        # entangled with `state`'s business-data transaction: roll back
        # phase-A's uncommitted mutations on `state` first, so a concurrent
        # reader can never observe accounting/forwarder state that's about
        # to be discarded alongside a run already reported FAILED.
        state.rollback_database()
        provisioning.fail_apply_gateway_lock_acquisition(prepared, request, lock_exc)
        with order_state.transaction():
            fail_paid_purchase(command, type(lock_exc).__name__, order_state)
        raise
    except Exception as exc:
        # Reached either after provision_apply_gateway()'s own exception
        # handler already rolled back and marked the run FAILED (its
        # `except Exception: state.rollback_database(); raise` just
        # above), or after activate_paid_purchase()'s own commit failed
        # inside `with order_state.transaction():` -- whose except clause
        # already rolled back `state`'s pending mutations (state and
        # order_state share one `db: Session`) before re-raising. Either
        # way, rollback has already completed by the time we're here, so
        # marking the run FAILED now is always ordered correctly. For the
        # first case this is a harmless repeat of what provision_apply_
        # gateway() already recorded; for the second, it is the only place
        # that ever does -- without it, a commit failure at that exact
        # point would leave the run stuck at RUNNING forever rather than
        # FAILED, though it could never end up falsely SUCCEEDED (ADR-017
        # run-persistence-ordering revision, Major 2 test 2).
        with order_state.transaction():
            fail_paid_purchase(command, type(exc).__name__, order_state)
        provisioning.mark_run_failed(prepared.run_id, exc)
        raise
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
