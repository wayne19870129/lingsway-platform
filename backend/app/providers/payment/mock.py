from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from backend.app.providers.base import PaymentIntentDTO, WebhookResult
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockPaymentProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    confirmed_references: list[str] = field(default_factory=list)

    def create_intent(self, order: object) -> PaymentIntentDTO:
        raise_injected(self.failures, "create_intent")
        return PaymentIntentDTO("payment-mock-1", "pending", Decimal(), "USD")

    def confirm_manual(self, order: object, reference: str) -> None:
        raise_injected(self.failures, "confirm_manual")
        self.confirmed_references.append(reference)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookResult:
        raise_injected(self.failures, "verify_webhook")
        return WebhookResult(True, "webhook-mock-1")
