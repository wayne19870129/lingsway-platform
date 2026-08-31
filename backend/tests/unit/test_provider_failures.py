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
