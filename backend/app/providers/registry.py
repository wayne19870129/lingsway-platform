"""Provider assembly driven exclusively by :class:`Settings`."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType

from backend.app.core.config import Settings
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    AccountingProvider,
    BlobStorage,
    CaptchaProvider,
    EgressProvider,
    EmailProvider,
    ForwarderProvider,
    GatewayProvider,
    NotifyProvider,
    PaymentProvider,
    TransportProvider,
)
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider


class ProviderConfigurationError(ValueError):
    """Raised when settings select an unavailable provider implementation."""


@dataclass(slots=True)
class ProviderRegistry:
    """The complete set of providers selected for one process/application
    lifetime.

    TASK-T16 Phase 2B8: this is also the sole ownership boundary for any
    resource a provider creates for itself (e.g. an ``httpx.Client`` a
    provider builds because none was injected). ``ProviderRegistry`` does
    not need to know which concrete providers own a closeable resource --
    :meth:`close` duck-types: any provider exposing a callable ``close``
    attribute gets it called once. Mock/noop providers (everything
    ``build_registry()`` assembles today) simply have no ``close`` method
    and are silently skipped, so this costs nothing before a real,
    resource-owning provider is ever wired in.

    Not a frozen dataclass (unlike before Phase 2B8): :meth:`close` must
    mutate ``_closed`` to stay idempotent. Nothing else about equality/
    construction changes for existing callers -- every field is still a
    plain keyword-constructible attribute.
    """

    egress: EgressProvider
    accounting: AccountingProvider
    gateway: GatewayProvider
    forwarder: ForwarderProvider
    payment: PaymentProvider
    notify: NotifyProvider
    email: EmailProvider
    captcha: CaptchaProvider
    storage: BlobStorage
    transport: TransportProvider
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        """Idempotently close every provider this registry holds that
        owns a closeable resource. Safe to call multiple times (a second
        call is a no-op) and safe to call even though most providers
        today have nothing to close."""
        if self._closed:
            return
        self._closed = True
        for provider in (
            self.egress,
            self.accounting,
            self.gateway,
            self.forwarder,
            self.payment,
            self.notify,
            self.email,
            self.captcha,
            self.storage,
            self.transport,
        ):
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()

    def __enter__(self) -> ProviderRegistry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


def build_registry(settings: Settings) -> ProviderRegistry:
    """Build all providers without network, Docker, filesystem, or shell access."""
    if settings.egress_provider != "mock":
        raise _unsupported("EGRESS_PROVIDER", settings.egress_provider)
    if settings.accounting_provider != "mock":
        raise _unsupported("ACCOUNTING_PROVIDER", settings.accounting_provider)
    if settings.gateway_provider != "mock":
        raise _unsupported("GATEWAY_PROVIDER", settings.gateway_provider)
    if settings.forwarder_provider != "mock":
        raise _unsupported("FORWARDER_PROVIDER", settings.forwarder_provider)
    if settings.payment_provider != "mock":
        raise _unsupported("PAYMENT_PROVIDER", settings.payment_provider)
    if settings.notify_provider != "noop":
        raise _unsupported("NOTIFY_PROVIDER", settings.notify_provider)
    if settings.email_provider != "noop":
        raise _unsupported("EMAIL_PROVIDER", settings.email_provider)
    if settings.captcha_provider != "noop":
        raise _unsupported("CAPTCHA_PROVIDER", settings.captcha_provider)
    if settings.storage_provider != "mock":
        raise _unsupported("STORAGE_PROVIDER", settings.storage_provider)
    if settings.transport_provider_mode != "mock":
        raise _unsupported("TRANSPORT_PROVIDER_MODE", settings.transport_provider_mode)

    return ProviderRegistry(
        egress=MockEgressProvider(),
        accounting=MockAccountingProvider(),
        gateway=MockGatewayProvider(),
        forwarder=MockForwarderProvider(),
        payment=MockPaymentProvider(),
        notify=NoopNotifyProvider(),
        email=NoopEmailProvider(),
        captcha=NoopCaptchaProvider(),
        storage=MockBlobStorage(),
        transport=MockTransportProvider(),
    )


def _unsupported(variable: str, value: str) -> ProviderConfigurationError:
    return ProviderConfigurationError(
        f"{variable}={value!r} is not implemented in T2; use the mock/noop selection"
    )
