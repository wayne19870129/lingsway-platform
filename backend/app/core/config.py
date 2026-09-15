"""Central settings merged from the provider registry and legacy runtime."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+pysqlite:///:memory:"
    jwt_secret: str = "development-only-not-for-production"
    secret_encryption_key: str = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    egress_provider: str = "mock"
    accounting_provider: str = "mock"
    gateway_provider: str = "mock"
    forwarder_provider: str = "mock"
    payment_provider: str = "mock"
    notify_provider: str = "noop"
    email_provider: str = "noop"
    captcha_provider: str = "noop"
    storage_provider: str = "mock"
    marzban_base_url: str = "https://marzban.example.invalid"
    marzban_admin_username: str = "CHANGE_ME"
    marzban_admin_password: str = "CHANGE_ME"
    marzban_default_protocol: str = "vless"
    marzban_default_inbounds_json: str = "{}"
    marzban_verify_tls: bool = True
    xray_reality_dest: str = ""
    xray_reality_server_name: str = ""
    accounting_sync_batch_size: int = 100
    grace_period_hours: int = 24
    subscription_base_url: str = "http://localhost:8000"
    subscription_domain: str = ""
    addon_20gb_price: Decimal = Decimal("0.00")
    addon_50gb_price: Decimal = Decimal("0.00")
    addon_100gb_price: Decimal = Decimal("0.00")
    plan_50gb_price: Decimal = Decimal("30.00")
    plan_100gb_price: Decimal = Decimal("50.00")
    plan_200gb_price: Decimal = Decimal("80.00")
    plan_500gb_price: Decimal = Decimal("150.00")
    plan_1000gb_price: Decimal = Decimal("200.00")
    webshare_total_quota_gb: Decimal = Decimal("250")
    webshare_ops_reserve_gb: Decimal = Decimal("20")
    admin_initial_email: str = "admin@example.com"
    admin_initial_password: str = "CHANGE_ME"
    transport_provider_mode: str = "mock"
    cors_origins: str = "http://localhost:3000"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        converted: dict[str, object] = {}
        decimal_names = {field.name for field in fields(cls) if field.type is Decimal}
        for field in fields(cls):
            raw = values.get(field.name.upper())
            if raw is None:
                continue
            if field.name in decimal_names:
                converted[field.name] = Decimal(raw)
            elif field.name in {"accounting_sync_batch_size", "grace_period_hours"}:
                converted[field.name] = int(raw)
            elif field.name == "marzban_verify_tls":
                converted[field.name] = raw.lower() in {"1", "true", "yes", "on"}
            else:
                converted[field.name] = raw
        settings = cls(**converted)  # type: ignore[arg-type]
        settings.validate_runtime_safety()
        return settings

    def validate_runtime_safety(self) -> None:
        if self.app_env == "production" and (
            self.jwt_secret.startswith("development-") or len(self.jwt_secret) < 32
        ):
            raise ValueError(
                "Production JWT_SECRET must contain at least 32 non-default characters"
            )
        if self.app_env == "production" and self.secret_encryption_key.startswith("MDAwMDAw"):
            raise ValueError("Production SECRET_ENCRYPTION_KEY must be configured")
        if self.app_env == "production" and self.accounting_provider == "marzban":
            if "example.invalid" in self.marzban_base_url:
                raise ValueError("Production MARZBAN_BASE_URL must be configured")
            if "CHANGE_ME" in {self.marzban_admin_username, self.marzban_admin_password}:
                raise ValueError("Production Marzban credentials must be configured")


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
