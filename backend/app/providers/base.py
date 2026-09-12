"""Provider contracts for every side effect used by the domain layer.

ADR-009 defines this boundary. Domain code may depend on these contracts and DTOs,
but never on a concrete provider implementation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EgressEndpointDTO:
    endpoint_id: str
    host: str
    port: int
    protocol: str = "socks5"
    public_ip: str | None = None


@dataclass(frozen=True, slots=True)
class CapacityDTO:
    total_gb: Decimal
    allocated_gb: Decimal
    reserved_gb: Decimal

    @property
    def available_gb(self) -> Decimal:
        return self.total_gb - self.allocated_gb - self.reserved_gb


@dataclass(frozen=True, slots=True)
class TenantDTO:
    tenant_id: str
    label: str
    quota_gb: Decimal
    thread_limit: int


@dataclass(frozen=True, slots=True)
class UsageDTO:
    tenant_id: str
    used_gb: Decimal
    measured_at: datetime
    used_bytes: int = 0
    status: str = "unknown"


@dataclass(frozen=True, slots=True)
class TransportEndpointDTO:
    external_id: str
    name: str
    region: str
    protocol: str
    host: str
    port: int
    transport: str | None = None
    tls_mode: str | None = None
    auth_secret_ref: str | None = None
    raw_metadata: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TransportCapacityDTO:
    quota_bytes: int
    used_bytes: int
    measured_at: datetime
    expire_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CredentialDTO:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReplacementDTO:
    endpoint_id: str
    replacement_endpoint_id: str | None
    dry_run: bool


@dataclass(frozen=True, slots=True)
class AccountUserDTO:
    """The accounting provider's own record of a created/updated user.

    ``routing_principal`` (ADR-016 Decision 3) is the Xray
    ``routing.rules[].user`` matching identity for this account -- for a
    real Marzban-backed implementation this is the patched
    ``UserResponse.routing_principal`` field
    (``f"{marzban_db_user_id}.{username}"``), never derived or guessed by
    this codebase. It is a **required, non-empty** field: there is no
    default, and callers must never fall back to ``username`` or an
    egress ``tenant_id`` when a real provider fails to supply it --
    ADR-016's "provider/API 不返回 principal 时怎么办" section requires
    treating that as a fail-closed ``CREATE_ACCOUNTING_USER`` failure
    instead. ``username`` remains the separate, independent accounting
    identity (``Subscription.accounting_user_id``) -- the two bounded
    contexts are never merged into one field.
    """

    username: str
    quota_bytes: int
    expire_at: datetime | None
    routing_principal: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class DesiredRoutingState:
    user_routes: Mapping[str, str] = field(default_factory=dict)
    outbound_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DesiredForwarderState:
    listeners: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    content: Mapping[str, object]
    version: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    applied: bool
    version: str
    rolled_back: bool = False


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentIntentDTO:
    intent_id: str
    status: str
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class WebhookResult:
    accepted: bool
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivered: bool
    delivery_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BlobDTO:
    key: str
    size: int
    checksum: str


class NotSupportedError(RuntimeError):
    """Raised when a provider deliberately does not support an operation."""


class EgressProvider(Protocol):
    name: str

    def list_endpoints(self) -> list[EgressEndpointDTO]: ...

    def capacity(self) -> CapacityDTO: ...

    def create_tenant(
        self, label: str, quota_gb: Decimal, thread_limit: int
    ) -> TenantDTO: ...

    def update_tenant_quota(self, tenant_id: str, quota_gb: Decimal) -> TenantDTO: ...

    def get_tenant_usage(self, tenant_id: str) -> UsageDTO: ...

    def get_credentials(self, tenant_id: str, endpoint_id: str) -> CredentialDTO: ...

    def replace_endpoint(
        self, endpoint_id: str, dry_run: bool = True
    ) -> ReplacementDTO: ...


class AccountingCreateEffect(Enum):
    """ADR-018: certainty classification for an ``AccountingProvider.
    create_user()`` failure's real external side effect. Domain
    compensation (``ProvisioningService``) branches on this instead of
    guessing from an HTTP status code or exception message.

    - ``NO_SIDE_EFFECT``: the provider has exact-source evidence the
      request was rejected before any user was created server-side (e.g.
      a duplicate-username conflict). Safe to fail closed with no
      compensation -- there is nothing to disable, and doing so anyway
      would blindly target an unrelated, pre-existing external account.
    - ``CREATED``: the provider has evidence a user *was* created for
      this request, but a postcondition (response contract) failed
      afterwards. Ownership is established -- compensation (disable,
      never delete) is required.
    - ``AMBIGUOUS``: the provider cannot prove which of the above
      happened (e.g. a transport failure after the request was
      dispatched). Never auto-retry the create, never blindly disable,
      never proceed past this step -- requires manual review.
    """

    NO_SIDE_EFFECT = "no_side_effect"
    CREATED = "created"
    AMBIGUOUS = "ambiguous"


class AccountingCreateUserError(RuntimeError):
    """ADR-018: raised by ``AccountingProvider.create_user()`` for a
    failure the provider can classify via :class:`AccountingCreateEffect`.
    This is the only typed contract across the provider/domain boundary
    for this method -- the domain layer depends on this exception and
    ``effect``, never on a concrete provider module's own exception
    hierarchy (e.g. ``backend.app.providers.accounting.marzban``, which
    the domain layer must never import).
    """

    def __init__(self, message: str, *, effect: AccountingCreateEffect) -> None:
        self.effect = effect
        super().__init__(message)


class AccountingProvider(Protocol):
    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        """Create an accounting user.

        ADR-018: when a failure's external-side-effect certainty can be
        classified, providers should raise :class:`AccountingCreateUserError`
        with the appropriate :class:`AccountingCreateEffect` rather than a
        provider-specific exception -- this lets
        ``ProvisioningService.provision_prepare()`` compensate safely
        (never blindly disabling a username that was never created by
        this call). A provider that raises any other exception is treated
        conservatively as if a user may have been created (``CREATED``).
        """
        ...

    def disable_user(self, username: str) -> None: ...

    def set_quota(self, username: str, quota_bytes: int) -> None: ...

    def set_expire(self, username: str, expire_at: datetime) -> None: ...

    def get_connection_links(self, username: str) -> list[str]: ...

    def get_usage(self, username: str) -> UsageDTO: ...


class TransportProvider(Protocol):
    def health_check(self) -> bool: ...

    def sync_nodes(self) -> None: ...

    def list_endpoints(self) -> list[TransportEndpointDTO]: ...

    def get_endpoint(self, external_id: str) -> TransportEndpointDTO | None: ...

    def enable(self) -> None: ...

    def disable(self) -> None: ...

    def get_capacity(self) -> TransportCapacityDTO | None: ...


class GatewayProvider(Protocol):
    def render(self, desired: DesiredRoutingState) -> CandidateConfig: ...

    def validate(self, candidate: CandidateConfig) -> ValidationResult: ...

    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...

    def health(self) -> HealthReport: ...


class ForwarderProvider(Protocol):
    def render(self, desired: DesiredForwarderState) -> CandidateConfig: ...

    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...

    def health(self) -> HealthReport: ...


class PaymentProvider(Protocol):
    def create_intent(self, order: object) -> PaymentIntentDTO: ...

    def confirm_manual(self, order: object, reference: str) -> None: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookResult: ...


class NotifyProvider(Protocol):
    def send(self, event: NotifyEvent) -> DeliveryResult: ...


class EmailProvider(Protocol):
    def send(self, to: str, template: str, ctx: dict[str, object]) -> DeliveryResult: ...


class CaptchaProvider(Protocol):
    def verify(self, token: str, remote_ip: str | None) -> bool: ...


class BlobStorage(Protocol):
    def put(self, key: str, path: str) -> None: ...

    def list(self, prefix: str) -> list[BlobDTO]: ...

    def delete(self, key: str) -> None: ...

    def checksum(self, key: str) -> str: ...
