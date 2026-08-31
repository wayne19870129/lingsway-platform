"""Provider assembly driven exclusively by :class:`Settings`."""

from dataclasses import dataclass

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
)
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.storage.mock import MockBlobStorage


class ProviderConfigurationError(ValueError):
    """Raised when settings select an unavailable provider implementation."""


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    egress: EgressProvider
    accounting: AccountingProvider
    gateway: GatewayProvider
    forwarder: ForwarderProvider
    payment: PaymentProvider
    notify: NotifyProvider
    email: EmailProvider
    captcha: CaptchaProvider
    storage: BlobStorage


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
    )


def _unsupported(variable: str, value: str) -> ProviderConfigurationError:
    return ProviderConfigurationError(
        f"{variable}={value!r} is not implemented in T2; use the mock/noop selection"
    )
