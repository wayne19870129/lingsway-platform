"""Central application settings.

Only this module reads provider-selection environment variables. Registry assembly
receives a Settings instance and never reads the environment directly.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    egress_provider: str = "mock"
    accounting_provider: str = "mock"
    gateway_provider: str = "mock"
    forwarder_provider: str = "mock"
    payment_provider: str = "mock"
    notify_provider: str = "noop"
    email_provider: str = "noop"
    captcha_provider: str = "noop"
    storage_provider: str = "mock"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        return cls(
            egress_provider=values.get("EGRESS_PROVIDER", "mock"),
            accounting_provider=values.get("ACCOUNTING_PROVIDER", "mock"),
            gateway_provider=values.get("GATEWAY_PROVIDER", "mock"),
            forwarder_provider=values.get("FORWARDER_PROVIDER", "mock"),
            payment_provider=values.get("PAYMENT_PROVIDER", "mock"),
            notify_provider=values.get("NOTIFY_PROVIDER", "noop"),
            email_provider=values.get("EMAIL_PROVIDER", "noop"),
            captcha_provider=values.get("CAPTCHA_PROVIDER", "noop"),
            storage_provider=values.get("STORAGE_PROVIDER", "mock"),
        )
