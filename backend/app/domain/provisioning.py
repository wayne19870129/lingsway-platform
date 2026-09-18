import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Protocol

from backend.app.domain.capacity import CapacityExceededError, ensure_capacity
from backend.app.domain.quota import quota_gb_to_bytes
from backend.app.providers.base import (
    AccountingCreateEffect,
    AccountingCreateUserError,
    AccountingProvider,
    AccountUserDTO,
    CredentialDTO,
    CredentialResolver,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    EgressProvider,
    ForwarderProvider,
    GatewayProvider,
    NotifyEvent,
    NotifyProvider,
    TenantDTO,
)


class ProvisionStatus(StrEnum):
    RUNNING = "RUNNING"
    PENDING_MANUAL = "PENDING_MANUAL"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


class ProvisionStep(IntEnum):
    CAPACITY = 1
    ALLOCATE_ENDPOINT = 2
    CREATE_TENANT = 3
    STORE_CREDENTIALS = 4
    APPLY_FORWARDER = 5
    CREATE_ACCOUNTING_USER = 6
    APPLY_GATEWAY = 7
    ISSUE_SUBSCRIPTION = 8
    NOTIFY = 9


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    order_id: str
    customer_id: str
    username: str
    quota_gb: Decimal
    thread_limit: int
    expire_at: datetime | None
    subscription_domain: str


class PendingManualReason(StrEnum):
    """ADR-018 (Major 2, second independent-review round): the
    business-facing *category* of a phase-A ``PENDING_MANUAL`` outcome --
    kept entirely separate from ``ProvisionOutcome.pending_manual_error``
    (a free-text, run-level diagnostic). ``services.
    confirm_payment_and_provision()`` maps this enum to a fixed, safe,
    persistable message for ``Subscription.provision_error`` via
    :data:`PENDING_MANUAL_BUSINESS_MESSAGES` -- it never persists
    provider exception text (which could, for a future provider, embed
    more than this repository has audited) as business data, and it
    never hardcodes every ``PENDING_MANUAL`` as "external tenant
    creation" the way it did before this enum existed.
    """

    EXTERNAL_TENANT_CREATION = "EXTERNAL_TENANT_CREATION"
    ACCOUNTING_CREATE_AMBIGUOUS = "ACCOUNTING_CREATE_AMBIGUOUS"
    ACCOUNTING_COMPENSATION_FAILED = "ACCOUNTING_COMPENSATION_FAILED"
    # ADR-026: the two compensation actions outside CREATE_ACCOUNTING_USER
    # that can themselves fail, leaving a real external side effect that
    # nothing has undone. Reported as PENDING_MANUAL for the same reason
    # ACCOUNTING_COMPENSATION_FAILED is -- a plain FAILED would claim the
    # run was fully compensated when it was not.
    FORWARDER_COMPENSATION_FAILED = "FORWARDER_COMPENSATION_FAILED"
    GATEWAY_COMPENSATION_FAILED = "GATEWAY_COMPENSATION_FAILED"


#: Fixed, safe, business-facing messages for each :class:`PendingManualReason`
#: -- never a provider exception's own text, never dynamic, never
#: containing a password/token/link/full HTTP body. This is what
#: ``services.confirm_payment_and_provision()`` persists into
#: ``Subscription.provision_error``; ``ProvisionOutcome.pending_manual_error``
#: (free text, run-level diagnostic only) is never used for this purpose.
PENDING_MANUAL_BUSINESS_MESSAGES: Mapping[PendingManualReason, str] = {
    PendingManualReason.EXTERNAL_TENANT_CREATION: (
        "external tenant creation requires review"
    ),
    PendingManualReason.ACCOUNTING_CREATE_AMBIGUOUS: (
        "accounting user creation result is ambiguous and requires review"
    ),
    PendingManualReason.ACCOUNTING_COMPENSATION_FAILED: (
        "accounting user creation failed and automatic compensation "
        "(disable) also failed; requires manual review"
    ),
    PendingManualReason.FORWARDER_COMPENSATION_FAILED: (
        "forwarder configuration apply failed and restoring the previous "
        "configuration also failed; forwarder runtime state is unknown and "
        "requires manual review"
    ),
    PendingManualReason.GATEWAY_COMPENSATION_FAILED: (
        "gateway apply failed and automatic compensation (disable) also "
        "failed; the accounting user may still be enabled and requires "
        "manual review"
    ),
}


@dataclass(frozen=True, slots=True)
class ProvisionOutcome:
    run_id: str
    status: ProvisionStatus
    subscription_url: str | None = None
    #: Set only when ``status`` is ``PENDING_MANUAL`` -- a free-text,
    #: run-level diagnostic (the original error text from
    #: ``ExternalTenantCreationError``, an ambiguous accounting-create
    #: failure, or a failed compensation attempt), preserved here so the
    #: caller can persist it on the run's terminal status *after* its own
    #: business-data commit completes, without provision_prepare() having
    #: to durably write it first (ADR-017 run-persistence-ordering
    #: revision -- see ``ProvisioningService.mark_run_pending_manual``).
    #: This is a diagnostic aid only -- never the source of the
    #: business-facing ``Subscription.provision_error`` message (see
    #: ``reason``/``PENDING_MANUAL_BUSINESS_MESSAGES``, ADR-018 Major 2).
    pending_manual_error: str | None = None
    #: Set only when ``status`` is ``PENDING_MANUAL`` -- the typed,
    #: closed-set business category (ADR-018 Major 2). ``services.
    #: confirm_payment_and_provision()`` looks this up in
    #: ``PENDING_MANUAL_BUSINESS_MESSAGES`` for the fixed, safe message it
    #: persists to ``Subscription.provision_error`` -- it never parses or
    #: otherwise relies on ``pending_manual_error``'s free text for that.
    reason: PendingManualReason | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningCheckpoint:
    """Opaque continuation between :meth:`ProvisioningService.provision_prepare`
    (steps 1-6, run without the ADR-016 named lock held) and
    :meth:`ProvisioningService.provision_apply_gateway` (steps 7-9, which
    the caller must run entirely inside ``state.gateway_route_binding_lock()``).

    See ADR-017 for why this phase split exists and why the lock cannot
    simply be moved into ``APPLY_GATEWAY``'s own ``try`` block instead.

    ``account_user`` (ADR-016 Decision 3) carries ``CREATE_ACCOUNTING_USER``'s
    already fail-closed-validated return value across this phase boundary
    -- ``APPLY_GATEWAY`` needs ``account_user.routing_principal`` to pass
    to ``desired_routing_state()``, and must never recompute or guess it.
    The full DTO is kept (not just the principal string) since ADR-016
    requires this method's return value be preserved end to end, not
    reduced to a single field a future caller might need more of.
    """

    run_id: str
    endpoint: EgressEndpointDTO
    tenant: TenantDTO
    account_user: AccountUserDTO


@dataclass(frozen=True, slots=True)
class ProvisionRunRecord:
    run_id: str
    step: ProvisionStep
    status: ProvisionStatus
    error: str | None = None
    external_ids: Mapping[str, str] = field(default_factory=dict)


class ExternalTenantCreationError(RuntimeError):
    """The provider created a tenant but could not confirm the full operation."""

    def __init__(self, message: str, external_id: str | None = None) -> None:
        self.external_id = external_id
        super().__init__(message)


class ProvisionRunStore(Protocol):
    """Persist saga progress and terminal status.

    Ownership contract (ADR-017, run-persistence-ordering revision) --
    every implementation (production: ``_SqlAlchemyProvisionRuns``; tests:
    ``_Runs``/``FakeRuns``) must uphold this, since ``ProvisioningService``
    relies on it to keep external-side-effect compensation and run
    bookkeeping from interfering with each other:

    - :meth:`start` creates the run's identity. Called before any external
      side effect; a failure here is safe to propagate (nothing to
      compensate yet).
    - :meth:`record_step`, :meth:`record_external_id`, and
      :meth:`mark_notification_failed` are **best-effort progress
      bookkeeping only** -- non-terminal, informational. Implementations
      must never let a failure here propagate to the caller: an
      already-completed external side effect (a created accounting user,
      an applied gateway change) must never go uncompensated just because
      writing a progress marker about it failed. A caller that needs to
      run compensation logic after one of these calls must be able to
      assume it always returns normally.
    - :meth:`mark_status` sets the run's *terminal* status (``FAILED`` /
      ``SUCCEEDED`` / ``PENDING_MANUAL``) and is allowed to raise on
      failure -- callers are responsible for sequencing this call so it
      never runs ahead of the real terminal business state it is
      reporting on: ``FAILED`` only after the caller's own
      ``state.rollback_database()`` for that failure has completed;
      ``SUCCEEDED`` only after the caller's own business-data commit has
      completed (see :meth:`ProvisioningService.mark_apply_gateway_succeeded`).
    """

    def start(self, request: ProvisionRequest) -> str: ...

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None: ...

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None: ...

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None: ...

    def mark_notification_failed(self, run_id: str, error: str) -> None: ...


class ProvisioningState(Protocol):
    """Domain-facing DB state.

    ``gateway_route_binding_lock()`` exists per ADR-016 ("Decision 3 / Xray
    route identity architecture unblock"), which mandates a single MySQL
    named advisory lock as the only correct concurrency contract for
    ``GatewayRouteBinding`` writers -- ``GatewayRouteBinding`` has no index
    supporting the predicate its writers filter on, so a plain
    ``SELECT ... FOR UPDATE`` cannot serialize them.
    """

    def reject_capacity(self, order_id: str, reason: str) -> None: ...

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO: ...

    def release_endpoint(self, endpoint_id: str) -> None: ...

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str: ...

    def rollback_database(self) -> None: ...

    def gateway_route_binding_lock(self) -> AbstractContextManager[None]:
        """ADR-016 Decision 3: the mandatory named-lock concurrency
        contract for GatewayRouteBinding. Callers must perform
        desired_routing_state()'s GatewayRouteBinding mutation, and their
        own eventual DB commit/rollback for it, entirely inside this
        context -- see backend/app/infra/gateway_route_lock.py for why the
        lock must be held across that full span, not just the mutation
        itself."""
        ...

    def current_forwarder_state(self) -> DesiredForwarderState: ...

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState: ...

    def desired_routing_state(
        self,
        request: ProvisionRequest,
        endpoint: EgressEndpointDTO,
        tenant: TenantDTO,
        routing_principal: str,
    ) -> DesiredRoutingState:
        """ADR-016 Decision 3: ``routing_principal`` (the accounting
        provider's own ``AccountUserDTO.routing_principal``, carried via
        ``ProvisioningCheckpoint.account_user``) is the only legitimate
        source for ``GatewayRouteBinding.gateway_principal`` and for the
        ``DesiredRoutingState.user_routes`` key Xray actually matches on
        -- implementations must not derive, guess, or fall back to
        ``request.username``/an egress ``tenant.tenant_id`` here."""
        ...

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None: ...


@dataclass(slots=True)
class ProvisioningService:
    """Provisioning orchestration with ADR-019's resolver dependency."""

    egress: EgressProvider
    accounting: AccountingProvider
    gateway: GatewayProvider
    credential_resolver: CredentialResolver
    forwarder: ForwarderProvider
    notify: NotifyProvider
    state: ProvisioningState
    runs: ProvisionRunStore
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)

    def provision(self, request: ProvisionRequest) -> ProvisionOutcome:
        """Run the full nine-step saga in one call.

        A thin composition of :meth:`provision_prepare` and
        :meth:`provision_apply_gateway` kept for callers that do not need
        the ADR-017 phase split (e.g. calling this directly against a
        non-locking test double). Production's paid-purchase path
        (``services.confirm_payment_and_provision``) calls the two phases
        separately so it can acquire the named lock only around the
        second one -- see ADR-017. This composition has no separate
        business-data commit step of its own (unlike the paid-purchase
        path's ``activate_paid_purchase``/``mark_provision_pending``), so
        :meth:`mark_apply_gateway_succeeded`/:meth:`mark_run_pending_manual`
        are called immediately once ``provision_prepare``/
        ``provision_apply_gateway`` return -- there is nothing else to
        wait for in this path.
        """
        prepared = self.provision_prepare(request)
        if isinstance(prepared, ProvisionOutcome):
            self.mark_run_pending_manual(prepared.run_id, prepared.pending_manual_error)
            return prepared
        outcome = self.provision_apply_gateway(prepared, request)
        if outcome.status is ProvisionStatus.PENDING_MANUAL:
            # ADR-026: phase B can now also end PENDING_MANUAL (gateway
            # compensation failed). That is not a success, so it must never
            # be marked SUCCEEDED.
            self.mark_run_pending_manual(outcome.run_id, outcome.pending_manual_error)
            return outcome
        self.mark_apply_gateway_succeeded(outcome.run_id)
        return outcome

    def provision_prepare(
        self, request: ProvisionRequest
    ) -> ProvisioningCheckpoint | ProvisionOutcome:
        """Steps 1-6 (``CAPACITY`` through ``CREATE_ACCOUNTING_USER``).

        Never touches ``GatewayRouteBinding`` and never acquires the
        ADR-016 named lock -- see ADR-017. Returns a
        :class:`ProvisioningCheckpoint` to continue with
        :meth:`provision_apply_gateway`, or a terminal
        :class:`ProvisionOutcome` (``PENDING_MANUAL``) that callers must
        not continue past. Any other failure propagates as an exception,
        exactly as before this method existed.
        """
        run_id = self.runs.start(request)

        self._start(run_id, ProvisionStep.CAPACITY)
        try:
            ensure_capacity(self.egress.capacity(), request.quota_gb)
        except CapacityExceededError as exc:
            self.state.reject_capacity(request.order_id, str(exc))
            self._alert("CAPACITY_EXCEEDED", {"run_id": run_id, "order_id": request.order_id})
            # Nothing has been flushed yet at this step, but rolling back
            # unconditionally before _failed() -- rather than only where a
            # mutation is known to exist -- keeps every failure branch in
            # this method following one uniform rule (see ADR-017's
            # run-persistence-ordering revision): the run's FAILED status
            # is never persisted ahead of the business rollback for the
            # same failure.
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.CAPACITY, exc)
            raise
        self._success(run_id, ProvisionStep.CAPACITY)

        self._start(run_id, ProvisionStep.ALLOCATE_ENDPOINT)
        try:
            endpoint = self.state.allocate_endpoint(request.customer_id)
        except Exception as exc:
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.ALLOCATE_ENDPOINT, exc)
            raise
        self._success(run_id, ProvisionStep.ALLOCATE_ENDPOINT)

        self._start(run_id, ProvisionStep.CREATE_TENANT)
        try:
            tenant = self.egress.create_tenant(
                request.username, request.quota_gb, request.thread_limit
            )
        except ExternalTenantCreationError as exc:
            # Best-effort progress bookkeeping only (record_external_id/
            # record_step already swallow their own persistence failures
            # per ProvisionRunStore's contract) -- the terminal
            # PENDING_MANUAL status is deliberately *not* durably written
            # here. This is a legitimate, real business outcome (external
            # tenant creation result is unconfirmed, needs manual review),
            # not a failure: persisting it before the caller's own
            # business-data commit (order_state.mark_provision_pending())
            # would let the run's terminal status race ahead of the real
            # business state, exactly like the SUCCEEDED case (ADR-017
            # run-persistence-ordering revision) -- worse, if this write
            # itself failed here, the resulting exception would propagate
            # out of provision_prepare() and be caught by the caller's
            # generic failure handler, mislabeling a manual-review outcome
            # as PROVISION_FAILED. See
            # ProvisioningService.mark_run_pending_manual().
            if exc.external_id is not None:
                self.runs.record_external_id(run_id, "egress_tenant", exc.external_id)
            self.runs.record_step(run_id, ProvisionStep.CREATE_TENANT, "PENDING_MANUAL")
            return ProvisionOutcome(
                run_id,
                ProvisionStatus.PENDING_MANUAL,
                pending_manual_error=str(exc),
                reason=PendingManualReason.EXTERNAL_TENANT_CREATION,
            )
        except Exception as exc:
            self.runs.record_step(run_id, ProvisionStep.CREATE_TENANT, "PENDING_MANUAL")
            return ProvisionOutcome(
                run_id,
                ProvisionStatus.PENDING_MANUAL,
                pending_manual_error=str(exc),
                reason=PendingManualReason.EXTERNAL_TENANT_CREATION,
            )
        self.runs.record_external_id(run_id, "egress_tenant", tenant.tenant_id)
        self._success(run_id, ProvisionStep.CREATE_TENANT)

        self._start(run_id, ProvisionStep.STORE_CREDENTIALS)
        try:
            credential = self.egress.get_credentials(tenant.tenant_id, endpoint.endpoint_id)
            secret_ref = self.state.save_credentials(tenant, endpoint, credential)
        except Exception as exc:
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.STORE_CREDENTIALS, exc)
            raise
        self._success(run_id, ProvisionStep.STORE_CREDENTIALS)

        self._start(run_id, ProvisionStep.APPLY_FORWARDER)
        previous_forwarder = self.state.current_forwarder_state()
        try:
            desired_forwarder = self.state.desired_forwarder_state(
                endpoint, tenant, secret_ref
            )
            self.forwarder.apply(self.forwarder.render(desired_forwarder))
        except Exception as exc:
            try:
                self.forwarder.apply(self.forwarder.render(previous_forwarder))
            except Exception as restore_exc:
                # ADR-026: the compensating restore itself failed, so the
                # forwarder's runtime configuration is in an unknown state
                # -- this can never be reported as a plain, fully-compensated
                # FAILED. Same shape as CREATE_ACCOUNTING_USER's
                # compensation-failed branch (ADR-018): no rollback (the
                # partial state is what manual review needs to see, and the
                # external tenant created at CREATE_TENANT still exists), no
                # internal mark_status() (ADR-017 -- the caller persists the
                # terminal PENDING_MANUAL after its own business commit), and
                # only exception *type names* in the diagnostic, never raw
                # provider text.
                self.runs.record_step(run_id, ProvisionStep.APPLY_FORWARDER, "PENDING_MANUAL")
                return ProvisionOutcome(
                    run_id,
                    ProvisionStatus.PENDING_MANUAL,
                    pending_manual_error=(
                        f"forwarder apply failed ({type(exc).__name__}), and "
                        "restoring the previous forwarder configuration also "
                        f"failed ({type(restore_exc).__name__})"
                    ),
                    reason=PendingManualReason.FORWARDER_COMPENSATION_FAILED,
                )
            # STORE_CREDENTIALS already flushed this run's credential/
            # binding mutation -- roll it back before recording FAILED.
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.APPLY_FORWARDER, exc)
            raise
        self._success(run_id, ProvisionStep.APPLY_FORWARDER)

        self._start(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)
        try:
            account_user = self.accounting.create_user(
                request.username, quota_gb_to_bytes(request.quota_gb), request.expire_at
            )
            # ADR-016 Decision 3: fail-closed provider-contract validation.
            # The domain layer trusts only an already-validated DTO -- it
            # never computes a Marzban-style f"{id}.{username}" principal
            # itself (that formula is the real provider's responsibility)
            # and never falls back to request.username/an egress
            # tenant_id when the contract is violated. A postcondition
            # failure here means the provider's create_user() call already
            # returned successfully -- ownership is established, so this
            # is classified the same as ADR-018's CREATED effect.
            if account_user.username != request.username:
                raise AccountingCreateUserError(
                    "accounting provider contract violation: create_user() "
                    f"returned username {account_user.username!r}, expected "
                    f"{request.username!r}",
                    effect=AccountingCreateEffect.CREATED,
                )
            if not account_user.routing_principal.strip():
                raise AccountingCreateUserError(
                    "accounting provider contract violation: create_user() "
                    "returned an empty or whitespace-only routing_principal",
                    effect=AccountingCreateEffect.CREATED,
                )
        except AccountingCreateUserError as exc:
            if exc.effect is AccountingCreateEffect.NO_SIDE_EFFECT:
                # ADR-018: the provider has exact-source evidence no user
                # was ever created for this request -- disabling
                # request.username here would blindly target an
                # unrelated, pre-existing external account (e.g. a 409
                # username conflict). Skip compensation entirely.
                self.state.rollback_database()
                self._failed(run_id, ProvisionStep.CREATE_ACCOUNTING_USER, exc)
                raise
            if exc.effect is AccountingCreateEffect.AMBIGUOUS:
                # ADR-018: the provider cannot prove whether a user was
                # created (e.g. a transport failure after dispatch) --
                # never blind-disable, never retry the create, never
                # proceed to APPLY_GATEWAY. This mirrors CREATE_TENANT's
                # existing PENDING_MANUAL pattern exactly (ADR-017): no
                # internal mark_status() here, only best-effort progress
                # bookkeeping, so the terminal PENDING_MANUAL status is
                # only ever durably recorded by the caller after its own
                # business commit (services.confirm_payment_and_provision()
                # already handles this generically for any phase-A
                # ProvisionOutcome).
                self.runs.record_step(
                    run_id, ProvisionStep.CREATE_ACCOUNTING_USER, "PENDING_MANUAL"
                )
                return ProvisionOutcome(
                    run_id,
                    ProvisionStatus.PENDING_MANUAL,
                    pending_manual_error=(
                        f"accounting user creation result is ambiguous: {exc}"
                    ),
                    reason=PendingManualReason.ACCOUNTING_CREATE_AMBIGUOUS,
                )
            # CREATED: ownership is established -- compensate.
            return self._compensate_created_accounting_user(run_id, request, exc)
        except Exception as exc:
            # A provider that has not adopted ADR-018's typed contract
            # (or a genuine bug) -- treated conservatively as if a user
            # may have been created, preserving this method's pre-ADR-018
            # default behavior (always attempt to disable).
            return self._compensate_created_accounting_user(run_id, request, exc)
        self._success(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)

        return ProvisioningCheckpoint(
            run_id=run_id, endpoint=endpoint, tenant=tenant, account_user=account_user
        )

    def provision_apply_gateway(
        self, checkpoint: ProvisioningCheckpoint, request: ProvisionRequest
    ) -> ProvisionOutcome:
        """Steps 7-9 (``APPLY_GATEWAY`` through ``NOTIFY``).

        Callers **must** invoke this only from inside
        ``state.gateway_route_binding_lock()``, and must not release that
        lock until their own commit/rollback for this call's
        ``GatewayRouteBinding`` mutation has completed -- see ADR-017.
        This is the only method that calls ``desired_routing_state()``.

        ADR-016 Decision 3: ``checkpoint.account_user.routing_principal``
        (already fail-closed-validated by ``provision_prepare()``'s
        ``CREATE_ACCOUNTING_USER`` step) is passed explicitly to
        ``desired_routing_state()`` -- this method never re-derives it,
        never re-calls the accounting provider, and the infra layer
        underneath ``state`` never guesses it either.
        """
        run_id = checkpoint.run_id
        endpoint = checkpoint.endpoint
        tenant = checkpoint.tenant
        routing_principal = checkpoint.account_user.routing_principal

        self._start(run_id, ProvisionStep.APPLY_GATEWAY)
        try:
            desired_routing = self.state.desired_routing_state(
                request, endpoint, tenant, routing_principal
            )
            candidate = self.gateway.render(desired_routing, self.credential_resolver)
            validation = self.gateway.validate(candidate)
            if not validation.valid:
                errors = "; ".join(validation.errors)
                raise RuntimeError(f"gateway candidate validation failed: {errors}")
            self.gateway.apply(candidate)
        except Exception as exc:
            # Alert first: it is best-effort (never raises) and must fire
            # whether or not the compensation below succeeds.
            self._alert("GATEWAY_APPLY_FAILED", {"run_id": run_id, "user": request.username})
            try:
                self.accounting.disable_user(request.username)
            except Exception as disable_exc:
                # ADR-026: the accounting user created in phase A may still
                # be enabled. Never a plain FAILED -- identical treatment to
                # CREATE_ACCOUNTING_USER's compensation-failed branch
                # (ADR-018): no rollback, no internal mark_status(), and
                # only exception type names in the diagnostic. Callers must
                # branch on this status and must NOT proceed to activate the
                # purchase (see services.confirm_payment_and_provision).
                self.runs.record_step(run_id, ProvisionStep.APPLY_GATEWAY, "PENDING_MANUAL")
                return ProvisionOutcome(
                    run_id,
                    ProvisionStatus.PENDING_MANUAL,
                    pending_manual_error=(
                        f"gateway apply failed ({type(exc).__name__}), and "
                        "disabling the accounting user also failed "
                        f"({type(disable_exc).__name__})"
                    ),
                    reason=PendingManualReason.GATEWAY_COMPENSATION_FAILED,
                )
            # desired_routing_state() flushed (but never committed) a
            # GatewayRouteBinding mutation -- roll it back here, before
            # recording FAILED, rather than relying on the caller's own
            # rollback (still required, as a harmless no-op safety net,
            # while this call is holding the named lock -- see ADR-017).
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.APPLY_GATEWAY, exc)
            raise
        self._success(run_id, ProvisionStep.APPLY_GATEWAY)

        self._start(run_id, ProvisionStep.ISSUE_SUBSCRIPTION)
        try:
            raw_token = self.token_factory()
            self.state.store_subscription_token(request.customer_id, raw_token)
            subscription_url = _subscription_url(request.subscription_domain, raw_token)
        except Exception as exc:
            # Rolls back the same flushed-but-uncommitted GatewayRouteBinding
            # mutation as the APPLY_GATEWAY branch above -- this step runs
            # after APPLY_GATEWAY succeeded, so it is still pending.
            self.state.rollback_database()
            self._failed(run_id, ProvisionStep.ISSUE_SUBSCRIPTION, exc)
            raise
        self._success(run_id, ProvisionStep.ISSUE_SUBSCRIPTION)

        self._start(run_id, ProvisionStep.NOTIFY)
        try:
            self.notify.send(
                NotifyEvent(
                    "PROVISION_SUCCEEDED",
                    {"run_id": run_id, "customer_id": request.customer_id},
                )
            )
        except Exception as exc:
            self.runs.mark_notification_failed(run_id, str(exc))
        self._success(run_id, ProvisionStep.NOTIFY)
        # Terminal SUCCEEDED is deliberately *not* persisted here: at this
        # point the caller's own business-data commit (activate_paid_purchase,
        # for the paid-purchase saga) has not run yet. Persisting SUCCEEDED
        # now would let the Job's terminal status race ahead of the real
        # business outcome (ADR-017 run-persistence-ordering revision,
        # Major 2 Case A) -- if that commit then failed, the Job would be
        # durably SUCCEEDED while the order/subscription end up
        # PROVISION_FAILED. Callers must call
        # mark_apply_gateway_succeeded() themselves, only after their own
        # business commit has completed.
        return ProvisionOutcome(run_id, ProvisionStatus.SUCCEEDED, subscription_url)

    def mark_run_failed(self, run_id: str, error: Exception) -> None:
        """Persist the run's terminal FAILED status for a failure that
        happened entirely outside this service's own step handlers -- e.g.
        the caller's own business-data commit failing *after*
        :meth:`provision_apply_gateway` already returned successfully
        (ADR-017 run-persistence-ordering revision, Major 2 test 2: a
        commit failure at that point must never leave the run durably
        SUCCEEDED). Callers must call this only after their own
        ``state.rollback_database()`` (or equivalent, e.g. the rollback a
        failed ``order_state.transaction()`` already performs internally)
        for the failure has completed -- the same ordering contract as
        every other FAILED write in this class. Unlike
        :meth:`mark_apply_gateway_succeeded`, this is allowed to raise:
        see ``ProvisionRunStore``'s own contract for why FAILED remains a
        fully-raising write.
        """
        self.runs.mark_status(run_id, ProvisionStatus.FAILED, str(error))

    def mark_apply_gateway_succeeded(self, run_id: str) -> None:
        """Persist the run's terminal SUCCEEDED status.

        Callers **must** call this only after their own business-data
        commit for this run has already completed (ADR-017
        run-persistence-ordering revision) -- never before, and never
        speculatively. Best-effort: by the time a caller reaches this
        point, the business transaction it is reporting on has already
        durably committed, so a failure persisting this audit status must
        not be allowed to surface as a reported failure for an outcome
        that already succeeded for real.
        """
        try:
            self.runs.mark_status(run_id, ProvisionStatus.SUCCEEDED)
        except Exception:
            return

    def mark_run_pending_manual(self, run_id: str, error: str | None) -> None:
        """Persist the run's terminal PENDING_MANUAL status.

        Callers **must** call this only after their own business-data
        commit for the pending-manual outcome (``order_state.
        mark_provision_pending()`` in the paid-purchase saga) has already
        completed -- same ordering contract as
        :meth:`mark_apply_gateway_succeeded`. Best-effort, for the same
        reason: by the time a caller reaches this point, the real
        business truth (a legitimate manual-review state, not a failure)
        has already been durably recorded, so a failure persisting this
        audit status must never be allowed to look like that outcome
        needs to be undone or reinterpreted as ``FAILED``.
        """
        try:
            self.runs.mark_status(run_id, ProvisionStatus.PENDING_MANUAL, error)
        except Exception:
            return

    def fail_before_gateway_authoritative_commit(
        self, checkpoint: ProvisioningCheckpoint, request: ProvisionRequest, error: Exception
    ) -> ProvisionOutcome:
        """Reuse ADR-018 compensation before the first durable gateway commit.

        The caller must roll back its business Session before calling this
        method. A confirmed accounting user is disabled through the existing
        certainty-aware helper; an ambiguous disable result stays
        ``PENDING_MANUAL`` and is never treated as an ordinary failure.
        """
        return self._compensate_created_accounting_user(checkpoint.run_id, request, error)

    def fail_apply_gateway_lock_acquisition(
        self, checkpoint: ProvisioningCheckpoint, request: ProvisionRequest, error: Exception
    ) -> ProvisionOutcome | None:
        """Compensation for the ADR-017 lock-acquisition-failure scenario.

        Call this when ``state.gateway_route_binding_lock()`` itself
        raises (``GatewayRouteBindingLockError``) before
        :meth:`provision_apply_gateway` could run. Because
        ``provision_apply_gateway`` never started, its own
        ``APPLY_GATEWAY`` exception handler never ran either -- this
        performs the identical compensation (disable the accounting user
        created in phase A, raise the same alert, mark the run FAILED at
        the same step) so the two failure paths cannot semantically drift
        apart.

        The caller **must** call ``state.rollback_database()`` itself
        *before* calling this method, not after: this method's own
        ``_failed()`` call durably persists the run's terminal ``FAILED``
        status, which must never be observable ahead of the rollback for
        the same failure (ADR-017 run-persistence-ordering revision). See
        ``services.confirm_payment_and_provision()``'s
        ``except GatewayRouteBindingLockError`` branch for the actual
        call order: rollback, then this method, then the business-side
        ``fail_paid_purchase()``.

        ADR-026: returns ``None`` when compensation succeeded (the run is
        durably ``FAILED`` and the caller should continue its own failure
        path), or a ``PENDING_MANUAL`` :class:`ProvisionOutcome` when
        ``disable_user()`` itself failed -- keeping this path semantically
        identical to ``provision_apply_gateway``'s ``APPLY_GATEWAY``
        handler, which ADR-017 requires the two never to drift apart on.
        """
        self._alert(
            "GATEWAY_APPLY_FAILED", {"run_id": checkpoint.run_id, "user": request.username}
        )
        try:
            self.accounting.disable_user(request.username)
        except Exception as disable_exc:
            self.runs.record_step(
                checkpoint.run_id, ProvisionStep.APPLY_GATEWAY, "PENDING_MANUAL"
            )
            return ProvisionOutcome(
                checkpoint.run_id,
                ProvisionStatus.PENDING_MANUAL,
                pending_manual_error=(
                    f"gateway route lock acquisition failed ({type(error).__name__}), "
                    "and disabling the accounting user also failed "
                    f"({type(disable_exc).__name__})"
                ),
                reason=PendingManualReason.GATEWAY_COMPENSATION_FAILED,
            )
        self._failed(checkpoint.run_id, ProvisionStep.APPLY_GATEWAY, error)
        return None

    def _compensate_created_accounting_user(
        self, run_id: str, request: ProvisionRequest, exc: Exception
    ) -> ProvisionOutcome:
        """ADR-018 ``CREATED``-effect (and unclassified-exception)
        compensation for ``CREATE_ACCOUNTING_USER``: ownership of
        ``request.username`` is established (or cannot be ruled out), so
        attempt to disable it (never ``DELETE`` -- AGENTS.md 铁律 4).

        If disabling succeeds, this is a fully-compensated failure --
        unwind the DB transaction and raise, exactly as this step's
        failure handling has always behaved. If disabling itself fails,
        the external account may still be enabled: this can never be
        reported as a plain, fully-compensated ``FAILED`` run, so it
        becomes ``PENDING_MANUAL`` instead (same best-effort,
        caller-persists-after-its-own-commit pattern as
        ``CREATE_TENANT``/the ``AMBIGUOUS`` branch above -- see ADR-017).
        """
        try:
            self.accounting.disable_user(request.username)
        except Exception as disable_exc:
            self.runs.record_step(
                run_id, ProvisionStep.CREATE_ACCOUNTING_USER, "PENDING_MANUAL"
            )
            # Safe failure context only: the exception *type* of both the
            # original failure and the disable failure, never a raw
            # provider exception message re-embedded here -- this is a
            # run-level diagnostic (ProvisionOutcome.pending_manual_error),
            # not the business-facing Subscription.provision_error message
            # (that comes from `reason` + PENDING_MANUAL_BUSINESS_MESSAGES
            # below, never from this free-text field).
            return ProvisionOutcome(
                run_id,
                ProvisionStatus.PENDING_MANUAL,
                pending_manual_error=(
                    f"accounting user creation failed ({type(exc).__name__}), "
                    "and disabling the created user also failed "
                    f"({type(disable_exc).__name__})"
                ),
                reason=PendingManualReason.ACCOUNTING_COMPENSATION_FAILED,
            )
        self.state.rollback_database()
        self._failed(run_id, ProvisionStep.CREATE_ACCOUNTING_USER, exc)
        raise exc

    def _alert(self, event_type: str, payload: Mapping[str, object]) -> None:
        try:
            self.notify.send(NotifyEvent(event_type, payload))
        except Exception:
            return

    def _start(self, run_id: str, step: ProvisionStep) -> None:
        self.runs.record_step(run_id, step, "STARTED")

    def _success(self, run_id: str, step: ProvisionStep) -> None:
        self.runs.record_step(run_id, step, "SUCCEEDED")

    def _failed(self, run_id: str, step: ProvisionStep, error: Exception) -> None:
        self.runs.record_step(run_id, step, "FAILED")
        self.runs.mark_status(run_id, ProvisionStatus.FAILED, str(error))


def _subscription_url(domain: str, token: str) -> str:
    base = domain.strip().rstrip("/")
    if not base:
        raise ValueError("subscription domain is required")
    if "://" not in base:
        base = f"https://{base}"
    return f"{base}/s/{token}"
