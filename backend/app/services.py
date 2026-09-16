"""Application service composition.

Concrete integrations are selected once by ``providers.registry``.  This module
only composes those contracts with domain services; it does not instantiate a
provider implementation or contain an alternate provisioning sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

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
    PENDING_MANUAL_BUSINESS_MESSAGES,
    ProvisioningService,
    ProvisioningState,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionRunStore,
    ProvisionStep,
)
from backend.app.domain.subscription_render import RenderedSubscription, render_subscription
from backend.app.infra.gateway_route_lock import GatewayRouteBindingLockError
from backend.app.infra.gateway_reconciliation import (
    assert_no_unresolved_gateway_mutation,
    enqueue_gateway_reconciliation,
    reconcile_gateway_job_in_session,
)
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import Subscription
from backend.app.providers.base import CredentialResolver, NotifyEvent
from backend.app.providers.registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    """All application services sharing one registry-selected provider set."""

    providers: ProviderRegistry
    provisioning: ProvisioningService


def build_services(
    state: ProvisioningState,
    runs: ProvisionRunStore,
    *,
    providers: ProviderRegistry,
    credential_resolver: CredentialResolver,
) -> ServiceContainer:
    """Compose domain services from the caller-supplied, already-owned
    provider registry.

    TASK-T16 Phase 2B8 (independent-review Major 3): ``providers`` is
    required, not an optional fallback to a freshly built, unowned
    registry -- exactly one ``ProviderRegistry`` must exist per process
    (built and closed by ``main.py``'s FastAPI ``lifespan`` or the
    scheduler's ``main()``), and every caller here must be handed that
    same managed instance rather than being allowed to silently
    construct (and never close) its own.
    """
    registry = providers
    return ServiceContainer(
        providers=registry,
        provisioning=ProvisioningService(
            egress=registry.egress,
            accounting=registry.accounting,
            gateway=registry.gateway,
            credential_resolver=credential_resolver,
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
    providers: ProviderRegistry,
    credential_resolver: CredentialResolver,
) -> ProvisionOutcome:
    """Run the single supported T3 nine-step provisioning orchestration."""
    return build_services(
        state, runs, providers=providers, credential_resolver=credential_resolver
    ).provisioning.provision(request)


def _confirm_paid_purchase_durable(
    command: BillingCommand,
    request: ProvisionRequest,
    state: ProvisioningState,
    runs: ProvisionRunStore,
    order_state: OrderWorkflowState,
    provisioning: ProvisioningService,
    checkpoint: object,
    registry: ProviderRegistry,
) -> ProvisionOutcome:
    """Commit DB B plus a pending gateway intent before any runtime mutation."""
    db = getattr(order_state, "db", None)
    if db is None or not hasattr(checkpoint, "account_user"):
        raise RuntimeError("durable gateway reconciliation requires SQLAlchemy state")
    subscription = db.scalar(
        select(Subscription).where(Subscription.order_id == int(command.order_id))
    )
    if subscription is None:
        raise RuntimeError("Provisioning subscription was not created")
    committed = False
    try:
        with state.gateway_route_binding_lock():
            assert_no_unresolved_gateway_mutation(db, subscription.id)
            subscription.accounting_user_id = checkpoint.account_user.username
            raw_token = provisioning.token_factory()
            state.store_subscription_token(request.customer_id, raw_token)
            # This flushes/validates the active route skeleton, but the fresh
            # post-commit snapshot is intentionally built by the reconciler.
            state.desired_routing_state(
                request,
                checkpoint.endpoint,
                checkpoint.tenant,
                checkpoint.account_user.routing_principal,
            )
            job = enqueue_gateway_reconciliation(
                db,
                operation_kind="PURCHASE",
                subscription_id=subscription.id,
                order_id=int(command.order_id),
                provision_run_id=checkpoint.run_id,
                operation_id=command.order_id,
            )
            runs.record_step(checkpoint.run_id, ProvisionStep.APPLY_GATEWAY, "PENDING")
            db.commit()
            committed = True
            result = reconcile_gateway_job_in_session(db, job.id, registry)
        if result.applied:
            try:
                registry.notify.send(
                    NotifyEvent(
                        "PROVISION_SUCCEEDED",
                        {"run_id": checkpoint.run_id, "customer_id": request.customer_id},
                    )
                )
            except Exception as exc:
                runs.mark_notification_failed(checkpoint.run_id, type(exc).__name__)
            fresh_state = SqlAlchemyProvisioningState(db, subscription.id)
            return ProvisionOutcome(
                checkpoint.run_id,
                ProvisionStatus.SUCCEEDED,
                fresh_state.current_subscription_url(request.subscription_domain),
            )
        return ProvisionOutcome(checkpoint.run_id, ProvisionStatus.RUNNING)
    except Exception as exc:
        if not committed:
            db.rollback()
            with order_state.transaction():
                fail_paid_purchase(command, type(exc).__name__, order_state)
            provisioning.mark_run_failed(checkpoint.run_id, exc)
        raise


def confirm_payment_and_provision(
    command: BillingCommand,
    request: ProvisionRequest,
    order: object,
    state: ProvisioningState,
    runs: ProvisionRunStore,
    order_state: OrderWorkflowState,
    payment_reference: str,
    *,
    providers: ProviderRegistry,
    credential_resolver: CredentialResolver,
    payment_provider_code: str = "manual",
) -> ProvisionOutcome | None:
    """Run the three-phase paid-order workflow.

    Ordering owns DB transitions, while ``ProvisioningService`` owns only the
    external nine-step saga.  Each DB phase has its own transaction supplied by
    ``order_state``; no provider call is made while one is open.

    ``providers`` is required (TASK-T16 Phase 2B8, independent-review
    Major 3): the caller must supply the one process-managed
    ``ProviderRegistry`` it does not own -- this function must never
    construct (and thus never be responsible for closing) its own.
    """
    registry = providers
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
    provisioning = build_services(
        state, runs, providers=registry, credential_resolver=credential_resolver
    ).provisioning
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
        # ADR-018 Major 2: the business-facing Subscription.provision_error
        # message is looked up from prepared.reason's fixed, safe mapping
        # -- never hardcoded to "external tenant creation" (that was only
        # ever true for the original CREATE_TENANT PENDING_MANUAL source;
        # ADR-018 added two more: an ambiguous accounting create, and a
        # failed disable compensation) and never derived from
        # prepared.pending_manual_error's free-text provider diagnostic.
        business_message = (
            PENDING_MANUAL_BUSINESS_MESSAGES[prepared.reason]
            if prepared.reason is not None
            else "provisioning requires manual review"
        )
        with order_state.transaction():
            order_state.mark_provision_pending(command, business_message)
        # ADR-017 run-persistence-ordering revision: the run's terminal
        # PENDING_MANUAL status is only ever recorded here, after the
        # business commit above has actually completed -- never inside
        # provision_prepare() itself. mark_run_pending_manual() is
        # best-effort (never raises), so a Job-write hiccup at this point
        # cannot turn this already-committed, legitimate manual-review
        # business state into anything else.
        provisioning.mark_run_pending_manual(prepared.run_id, prepared.pending_manual_error)
        return prepared

    # Real SQLAlchemy paid provisioning follows ADR-022 Direction B. Test
    # doubles retain the legacy composition below so the domain contract stays
    # unchanged and does not gain an infrastructure/session dependency.
    if getattr(order_state, "db", None) is not None and hasattr(prepared, "account_user"):
        return _confirm_paid_purchase_durable(
            command,
            request,
            state,
            runs,
            order_state,
            provisioning,
            prepared,
            registry,
        )

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
    providers: ProviderRegistry,
) -> None:
    """Forward manual payment confirmation through the caller-supplied,
    already-owned provider registry (see ``confirm_payment_and_provision``'s
    docstring for why ``providers`` is required, not an optional
    self-constructing fallback)."""
    providers.payment.confirm_manual(order, reference)


def render_customer_subscription(
    links: list[str],
    user_agent: str,
) -> RenderedSubscription:
    """Keep subscription rendering in the domain renderer."""
    return render_subscription(links, user_agent)
