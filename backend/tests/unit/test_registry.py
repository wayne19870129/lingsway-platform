import socket
from dataclasses import dataclass, field

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
from backend.app.providers.registry import (
    ProviderConfigurationError,
    ProviderRegistry,
    build_registry,
)
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


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B8: ProviderRegistry ownership/lifecycle.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ClosableProvider:
    """A minimal stand-in for a future resource-owning provider (e.g. one
    holding an ``httpx.Client``): only tracks how many times ``close()``
    was called, so tests can assert exactly-once semantics without
    depending on any real provider's internals."""

    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)


def _all_mock_registry() -> ProviderRegistry:
    return build_registry(Settings())


def test_all_mock_registry_close_is_a_safe_noop() -> None:
    """None of build_registry()'s mock/noop providers own a closeable
    resource -- close() must not raise just because most providers here
    have no close() method at all."""
    registry = _all_mock_registry()
    registry.close()
    registry.close()  # idempotent -- second call is also a safe no-op.


def test_registry_close_calls_each_owned_providers_close_exactly_once() -> None:
    egress = _ClosableProvider()
    accounting = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=accounting,  # type: ignore[arg-type]
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    registry.close()

    assert len(egress.close_calls) == 1
    assert len(accounting.close_calls) == 1


def test_registry_close_is_idempotent_never_double_closes() -> None:
    egress = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    registry.close()
    registry.close()
    registry.close()

    assert len(egress.close_calls) == 1


def test_registry_context_manager_closes_on_exit() -> None:
    egress = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    with registry:
        assert egress.close_calls == []
    assert len(egress.close_calls) == 1
