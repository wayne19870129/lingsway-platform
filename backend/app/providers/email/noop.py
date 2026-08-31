from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.base import DeliveryResult
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class NoopEmailProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    deliveries: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def send(self, to: str, template: str, ctx: dict[str, object]) -> DeliveryResult:
        raise_injected(self.failures, "send")
        self.deliveries.append((to, template, dict(ctx)))
        return DeliveryResult(True, "email-noop")
