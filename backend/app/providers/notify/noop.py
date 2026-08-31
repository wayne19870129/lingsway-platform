from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.base import DeliveryResult, NotifyEvent
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class NoopNotifyProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    events: list[NotifyEvent] = field(default_factory=list)

    def send(self, event: NotifyEvent) -> DeliveryResult:
        raise_injected(self.failures, "send")
        self.events.append(event)
        return DeliveryResult(True, "notify-noop")
