import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Protocol

from backend.app.domain.capacity import CapacityExceededError, ensure_capacity
from backend.app.domain.quota import quota_gb_to_bytes
from backend.app.providers.base import (
    AccountingProvider,
    CredentialDTO,
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


@dataclass(frozen=True, slots=True)
class ProvisionOutcome:
    run_id: str
    status: ProvisionStatus
    subscription_url: str | None = None


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
    def start(self, request: ProvisionRequest) -> str: ...

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None: ...

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None: ...

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None: ...

    def mark_notification_failed(self, run_id: str, error: str) -> None: ...


class ProvisioningState(Protocol):
    def reject_capacity(self, order_id: str, reason: str) -> None: ...

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO: ...

    def release_endpoint(self, endpoint_id: str) -> None: ...

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str: ...

    def rollback_database(self) -> None: ...

    def current_forwarder_state(self) -> DesiredForwarderState: ...

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState: ...

    def desired_routing_state(
        self, request: ProvisionRequest, endpoint: EgressEndpointDTO, tenant: TenantDTO
    ) -> DesiredRoutingState: ...

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None: ...


@dataclass(slots=True)
class ProvisioningService:
    egress: EgressProvider
    accounting: AccountingProvider
    gateway: GatewayProvider
    forwarder: ForwarderProvider
    notify: NotifyProvider
    state: ProvisioningState
    runs: ProvisionRunStore
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)

    def provision(self, request: ProvisionRequest) -> ProvisionOutcome:
        run_id = self.runs.start(request)

        self._start(run_id, ProvisionStep.CAPACITY)
        try:
            ensure_capacity(self.egress.capacity(), request.quota_gb)
        except CapacityExceededError as exc:
            self.state.reject_capacity(request.order_id, str(exc))
            self._alert("CAPACITY_EXCEEDED", {"run_id": run_id, "order_id": request.order_id})
            self._failed(run_id, ProvisionStep.CAPACITY, exc)
            raise
        self._success(run_id, ProvisionStep.CAPACITY)

        self._start(run_id, ProvisionStep.ALLOCATE_ENDPOINT)
        try:
            endpoint = self.state.allocate_endpoint(request.customer_id)
        except Exception as exc:
            self._failed(run_id, ProvisionStep.ALLOCATE_ENDPOINT, exc)
            raise
        self._success(run_id, ProvisionStep.ALLOCATE_ENDPOINT)

        self._start(run_id, ProvisionStep.CREATE_TENANT)
        try:
            tenant = self.egress.create_tenant(
                request.username, request.quota_gb, request.thread_limit
            )
        except ExternalTenantCreationError as exc:
            if exc.external_id is not None:
                self.runs.record_external_id(run_id, "egress_tenant", exc.external_id)
            self.runs.record_step(run_id, ProvisionStep.CREATE_TENANT, "PENDING_MANUAL")
            self.runs.mark_status(run_id, ProvisionStatus.PENDING_MANUAL, str(exc))
            return ProvisionOutcome(run_id, ProvisionStatus.PENDING_MANUAL)
        except Exception as exc:
            self.runs.record_step(run_id, ProvisionStep.CREATE_TENANT, "PENDING_MANUAL")
            self.runs.mark_status(run_id, ProvisionStatus.PENDING_MANUAL, str(exc))
            return ProvisionOutcome(run_id, ProvisionStatus.PENDING_MANUAL)
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
            self.forwarder.apply(self.forwarder.render(previous_forwarder))
            self._failed(run_id, ProvisionStep.APPLY_FORWARDER, exc)
            raise
        self._success(run_id, ProvisionStep.APPLY_FORWARDER)

        self._start(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)
        try:
            self.accounting.create_user(
                request.username, quota_gb_to_bytes(request.quota_gb), request.expire_at
            )
        except Exception as exc:
            self.accounting.disable_user(request.username)
            self._failed(run_id, ProvisionStep.CREATE_ACCOUNTING_USER, exc)
            raise
        self._success(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)

        self._start(run_id, ProvisionStep.APPLY_GATEWAY)
        try:
            desired_routing = self.state.desired_routing_state(request, endpoint, tenant)
            candidate = self.gateway.render(desired_routing)
            validation = self.gateway.validate(candidate)
            if not validation.valid:
                errors = "; ".join(validation.errors)
                raise RuntimeError(f"gateway candidate validation failed: {errors}")
            self.gateway.apply(candidate)
        except Exception as exc:
            self.accounting.disable_user(request.username)
            self._alert("GATEWAY_APPLY_FAILED", {"run_id": run_id, "user": request.username})
            self._failed(run_id, ProvisionStep.APPLY_GATEWAY, exc)
            raise
        self._success(run_id, ProvisionStep.APPLY_GATEWAY)

        self._start(run_id, ProvisionStep.ISSUE_SUBSCRIPTION)
        try:
            raw_token = self.token_factory()
            self.state.store_subscription_token(request.customer_id, raw_token)
            subscription_url = _subscription_url(request.subscription_domain, raw_token)
        except Exception as exc:
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
        self.runs.mark_status(run_id, ProvisionStatus.SUCCEEDED)
        return ProvisionOutcome(run_id, ProvisionStatus.SUCCEEDED, subscription_url)

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
