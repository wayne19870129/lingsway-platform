"""Provider contracts for every side effect used by the domain layer.

ADR-009 defines this boundary. Domain code may depend on these contracts and DTOs,
but never on a concrete provider implementation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol, cast


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


class CredentialDTO:
    """Immutable credential value with an intentionally redacted repr."""

    __slots__ = ("_username", "_password")
    _username: str
    _password: str

    def __init__(self, username: str, password: str) -> None:
        object.__setattr__(self, "_username", username)
        object.__setattr__(self, "_password", password)

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return "CredentialDTO(username=<redacted>, password=<redacted>)"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CredentialDTO)
            and self.username == other.username
            and self.password == other.password
        )

    def __hash__(self) -> int:
        return hash((self.username, self.password))


class XrayOutboundDTO:
    """Immutable ref-only outbound value with a redacted secret reference."""

    __slots__ = ("_tag", "_host", "_port", "_protocol", "_credential_secret_ref")
    _tag: str
    _host: str
    _port: int
    _protocol: str
    _credential_secret_ref: str | None

    def __init__(
        self,
        tag: str,
        host: str,
        port: int,
        protocol: str,
        credential_secret_ref: str | None = None,
    ) -> None:
        object.__setattr__(self, "_tag", tag)
        object.__setattr__(self, "_host", host)
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_protocol", protocol)
        object.__setattr__(self, "_credential_secret_ref", credential_secret_ref)

    @property
    def tag(self) -> str:
        return self._tag

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def credential_secret_ref(self) -> str | None:
        return self._credential_secret_ref

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return (
            "XrayOutboundDTO("
            f"tag={self.tag!r}, host={self.host!r}, port={self.port!r}, "
            f"protocol={self.protocol!r}, credential_secret_ref=<redacted>)"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, XrayOutboundDTO)
            and self.tag == other.tag
            and self.host == other.host
            and self.port == other.port
            and self.protocol == other.protocol
            and self.credential_secret_ref == other.credential_secret_ref
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.tag,
                self.host,
                self.port,
                self.protocol,
                self.credential_secret_ref,
            )
        )


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
    outbounds: tuple[XrayOutboundDTO, ...] = ()


@dataclass(frozen=True, slots=True)
class ForwarderListenerDTO:
    name: str
    listener_type: str
    listen: str
    port: int
    proxy: str


@dataclass(frozen=True, slots=True)
class ForwarderEgressProxyDTO:
    name: str
    protocol: str
    host: str
    port: int
    credential_secret_ref: str = field(repr=False)
    credential_revision: int = 0
    transport_proxy_name: str | None = None
    transport_node_identity: tuple[str, str, int] | None = None


@dataclass(frozen=True, slots=True)
class EgressCredentialRequirement:
    """Render-stage opaque credential requirement."""

    proxy_name: str
    secret_ref: str = field(repr=False)
    revision: int


@dataclass(frozen=True, slots=True)
class EgressCredentialSnapshot:
    """Finalization-stage plaintext credential and fresh revision."""

    secret_ref: str = field(repr=False)
    revision: int
    credential: CredentialDTO = field(repr=False)


class EgressCredentialResolver(Protocol):
    def resolve_egress_credential(self, secret_ref: str) -> EgressCredentialSnapshot: ...


@dataclass(frozen=True, slots=True)
class DesiredForwarderState:
    listeners: Mapping[str, str] = field(default_factory=dict)
    listener_specs: tuple[ForwarderListenerDTO, ...] = ()
    # Provider-neutral full-projection inputs.  Mihomo-specific validation and
    # composition live at the forwarder boundary, never in the domain.
    proxies: tuple[Mapping[str, object], ...] = ()
    egress_proxies: tuple[ForwarderEgressProxyDTO, ...] = ()
    proxy_groups: tuple[Mapping[str, object], ...] = ()
    rules: tuple[Mapping[str, object], ...] = ()
    dns: Mapping[str, object] = field(default_factory=dict)
    policy: Mapping[str, object] = field(default_factory=dict)
    transport_materializations: tuple[object, ...] = ()
    transport_references: tuple[object, ...] = ()
    deployment_constants: Mapping[str, object] = field(default_factory=dict)
    snapshot_revision: int = 1
    snapshot_identity: str = "default"

    def __post_init__(self) -> None:
        if self.snapshot_revision <= 0 or not self.snapshot_identity.strip():
            raise ValueError("desired snapshot identity is invalid")

        def freeze(value: object) -> object:
            if isinstance(value, Mapping):
                return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, (str, int, float, bool, type(None))):
                return value
            raise TypeError("desired snapshot contains unsupported value")

        object.__setattr__(self, "listeners", freeze(self.listeners))
        if not all(isinstance(item, ForwarderListenerDTO) for item in self.listener_specs):
            raise TypeError("desired listener specs must use ForwarderListenerDTO")
        object.__setattr__(self, "listener_specs", tuple(self.listener_specs))
        object.__setattr__(self, "proxies", freeze(self.proxies))
        if not all(isinstance(item, ForwarderEgressProxyDTO) for item in self.egress_proxies):
            raise TypeError("desired egress proxies must use ForwarderEgressProxyDTO")
        object.__setattr__(self, "egress_proxies", tuple(self.egress_proxies))
        object.__setattr__(self, "proxy_groups", freeze(self.proxy_groups))
        object.__setattr__(self, "rules", freeze(self.rules))
        object.__setattr__(self, "dns", freeze(self.dns))
        object.__setattr__(self, "policy", freeze(self.policy))
        object.__setattr__(self, "deployment_constants", freeze(self.deployment_constants))

    def __repr__(self) -> str:
        return (
            "DesiredForwarderState("
            f"snapshot_revision={self.snapshot_revision!r}, "
            f"snapshot_identity={self.snapshot_identity!r}, "
            f"listener_count={len(self.listener_specs)}, proxy_count={len(self.proxies)}, "
            f"egress_proxy_count={len(self.egress_proxies)}, "
            f"proxy_group_count={len(self.proxy_groups)}, rule_count={len(self.rules)}, "
            f"content=<redacted>)"
        )


class CandidateConfig:
    """Immutable candidate whose generic dataclass serialization is unavailable."""

    __slots__ = ("_content", "_version")
    _content: Mapping[str, object]
    _version: str

    def __init__(self, content: Mapping[str, object], version: str) -> None:
        object.__setattr__(self, "_content", content)
        object.__setattr__(self, "_version", version)

    @property
    def content(self) -> Mapping[str, object]:
        return self._content

    @property
    def version(self) -> str:
        return self._version

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return f"CandidateConfig(version={self.version!r}, content=<redacted>)"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CandidateConfig)
            and self.content == other.content
            and self.version == other.version
        )


class ProjectionTemplate(CandidateConfig):
    """Secret-free, non-installable projection awaiting operation finalization."""

    __slots__ = (
        "_controller_secret_ref",
        "_controller_secret_revision",
        "_egress_credentials",
    )
    _controller_secret_ref: str
    _controller_secret_revision: int
    _egress_credentials: tuple[EgressCredentialRequirement, ...]

    def __init__(
        self,
        content: Mapping[str, object],
        version: str,
        controller_secret_ref: str,
        controller_secret_revision: int,
        egress_credentials: tuple[EgressCredentialRequirement, ...] = (),
    ) -> None:
        super().__init__(cast(Mapping[str, object], _freeze_content(content)), version)
        object.__setattr__(self, "_controller_secret_ref", controller_secret_ref)
        object.__setattr__(self, "_controller_secret_revision", controller_secret_revision)
        object.__setattr__(self, "_egress_credentials", tuple(egress_credentials))

    @property
    def controller_secret_ref(self) -> str:
        return self._controller_secret_ref

    @property
    def controller_secret_revision(self) -> int:
        return self._controller_secret_revision

    @property
    def egress_credentials(self) -> tuple[EgressCredentialRequirement, ...]:
        return self._egress_credentials

    def __repr__(self) -> str:
        return (
            "ProjectionTemplate("
            f"version={self.version!r}, controller_secret_ref=<redacted>, "
            f"controller_secret_revision={self.controller_secret_revision!r}, content=<redacted>)"
        )


def _freeze_content(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_content(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_content(item) for item in value)
    return value


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


_CREDENTIAL_ERROR_CODES = frozenset(
    {
        "CREDENTIAL_REF_INVALID",
        "CREDENTIAL_NOT_FOUND",
        "CREDENTIAL_DECRYPT_FAILED",
        "CREDENTIAL_MALFORMED",
        "CREDENTIAL_RESOLUTION_FAILED",
    }
)


class CredentialResolutionError(RuntimeError):
    """Stable, secret-safe failure contract for credential resolution."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _CREDENTIAL_ERROR_CODES else "CREDENTIAL_RESOLUTION_FAILED"
        self.code = safe_code
        super().__init__(safe_code)


class CredentialResolver(Protocol):
    """ADR-019 operation-scoped credential boundary for provider rendering."""

    def resolve(self, secret_ref: str) -> CredentialDTO: ...


class EgressProvider(Protocol):
    name: str

    def list_endpoints(self) -> list[EgressEndpointDTO]: ...

    def capacity(self) -> CapacityDTO: ...

    def create_tenant(self, label: str, quota_gb: Decimal, thread_limit: int) -> TenantDTO: ...

    def update_tenant_quota(self, tenant_id: str, quota_gb: Decimal) -> TenantDTO: ...

    def get_tenant_usage(self, tenant_id: str) -> UsageDTO: ...

    def get_credentials(self, tenant_id: str, endpoint_id: str) -> CredentialDTO: ...

    def replace_endpoint(self, endpoint_id: str, dry_run: bool = True) -> ReplacementDTO: ...


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
    def health_check(self) -> bool:
        """Check whether the accounting backend is reachable and usable."""
        ...

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
    """Provider contract whose resolver argument is required by ADR-019."""

    def render(
        self, desired: DesiredRoutingState, resolver: CredentialResolver
    ) -> CandidateConfig: ...

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
