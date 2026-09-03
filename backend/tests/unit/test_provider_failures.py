from datetime import UTC, datetime

import pytest

from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import NotifyEvent
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider


def injected_failure() -> RuntimeError:
    return RuntimeError("injected failure")


def test_egress_failure_can_be_injected() -> None:
    provider = MockEgressProvider(failures={"capacity": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.capacity()


def test_accounting_failure_can_be_injected() -> None:
    provider = MockAccountingProvider(failures={"get_usage": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.get_usage("user")


def test_accounting_contract_failure_can_be_injected() -> None:
    provider = MockAccountingProvider(failures={"set_quota": injected_failure()})
    provider.create_user("user", 1, None)
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.set_quota("user", 2)


def test_accounting_expiry_failure_can_be_injected() -> None:
    provider = MockAccountingProvider(failures={"set_expire": injected_failure()})
    provider.create_user("user", 1, None)
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.set_expire("user", datetime.now(UTC))


def test_accounting_usage_returns_bytes_and_status() -> None:
    provider = MockAccountingProvider()
    provider.create_user("user", 1, None)
    provider.set_mock_usage("user", 123)
    usage = provider.get_usage("user")
    assert usage.used_bytes == 123
    assert usage.status == "active"


def test_transport_failure_can_be_injected() -> None:
    provider = MockTransportProvider(failures={"sync_nodes": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.sync_nodes()


def test_gateway_failure_can_be_injected() -> None:
    provider = MockGatewayProvider(failures={"health": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.health()


def test_forwarder_failure_can_be_injected() -> None:
    provider = MockForwarderProvider(failures={"health": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.health()


def test_payment_failure_can_be_injected() -> None:
    provider = MockPaymentProvider(failures={"create_intent": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.create_intent(object())


def test_notify_failure_can_be_injected() -> None:
    provider = NoopNotifyProvider(failures={"send": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.send(NotifyEvent("test"))


def test_email_failure_can_be_injected() -> None:
    provider = NoopEmailProvider(failures={"send": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.send("test@example.invalid", "test", {})


def test_captcha_failure_can_be_injected() -> None:
    provider = NoopCaptchaProvider(failures={"verify": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.verify("token", None)


def test_storage_failure_can_be_injected() -> None:
    provider = MockBlobStorage(failures={"list": injected_failure()})
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.list("prefix")
