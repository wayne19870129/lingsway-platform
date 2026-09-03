import socket

import pytest

from backend.app.core.config import Settings
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.registry import ProviderConfigurationError, build_registry
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider


def test_all_mock_registry_assembles_without_network_or_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit registry attempted network access")

    monkeypatch.setattr(socket, "socket", reject_network)
    settings = Settings.from_env(
        {
            "EGRESS_PROVIDER": "mock",
            "ACCOUNTING_PROVIDER": "mock",
            "GATEWAY_PROVIDER": "mock",
            "FORWARDER_PROVIDER": "mock",
            "PAYMENT_PROVIDER": "mock",
            "NOTIFY_PROVIDER": "noop",
            "EMAIL_PROVIDER": "noop",
            "CAPTCHA_PROVIDER": "noop",
            "STORAGE_PROVIDER": "mock",
            "TRANSPORT_PROVIDER_MODE": "mock",
        }
    )

    registry = build_registry(settings)

    assert isinstance(registry.egress, MockEgressProvider)
    assert isinstance(registry.accounting, MockAccountingProvider)
    assert isinstance(registry.gateway, MockGatewayProvider)
    assert isinstance(registry.forwarder, MockForwarderProvider)
    assert isinstance(registry.payment, MockPaymentProvider)
    assert isinstance(registry.notify, NoopNotifyProvider)
    assert isinstance(registry.email, NoopEmailProvider)
    assert isinstance(registry.captcha, NoopCaptchaProvider)
    assert isinstance(registry.storage, MockBlobStorage)
    assert isinstance(registry.transport, MockTransportProvider)


def test_registry_rejects_unimplemented_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="EGRESS_PROVIDER"):
        build_registry(Settings(egress_provider="webshare"))


def test_registry_rejects_unimplemented_transport_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="TRANSPORT_PROVIDER_MODE"):
        build_registry(Settings(transport_provider_mode="subscription"))
