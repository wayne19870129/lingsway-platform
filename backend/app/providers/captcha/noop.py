from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class NoopCaptchaProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    accepted: bool = True

    def verify(self, token: str, remote_ip: str | None) -> bool:
        raise_injected(self.failures, "verify")
        return self.accepted
