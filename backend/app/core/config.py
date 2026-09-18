"""Central settings merged from the provider registry and legacy runtime."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit


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
    marzban_ca_cert_path: str = "/app/data/marzban/internal.crt"
    xray_config_path: str = "/app/data/marzban/xray_config.json"
    xray_backup_dir: str = "/app/data/xray-reload"
    xray_log_level: str = "warning"
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
    transport_cache_root: str = "/app/data/transport"
    cors_origins: str = "http://localhost:3000"

    def __post_init__(self) -> None:
        if not isinstance(self.xray_log_level, str):
            raise ValueError("XRAY_LOG_LEVEL must be a string")
        normalized = self.xray_log_level.strip().lower()
        if normalized not in {"debug", "info", "warning", "error", "none"}:
            raise ValueError("XRAY_LOG_LEVEL must be one of debug, info, warning, error, none")
        object.__setattr__(self, "xray_log_level", normalized)

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
            elif field.name == "xray_log_level":
                converted[field.name] = raw.strip().lower()
            else:
                converted[field.name] = raw
        settings = cls(**converted)  # type: ignore[arg-type]
        settings.validate_runtime_safety()
        return settings

    def validate_runtime_safety(self) -> None:
        if self.transport_provider_mode == "subscription" and not self.transport_cache_root.strip():
            raise ValueError(
                "TRANSPORT_CACHE_ROOT must not be blank when subscription transport is selected"
            )
        if self.app_env == "production" and (
            self.jwt_secret.startswith("development-") or len(self.jwt_secret) < 32
        ):
            raise ValueError(
                "Production JWT_SECRET must contain at least 32 non-default characters"
            )
        if self.app_env == "production" and self.secret_encryption_key.startswith("MDAwMDAw"):
            raise ValueError("Production SECRET_ENCRYPTION_KEY must be configured")
        marzban_runtime_selected = (
            self.accounting_provider == "marzban" or self.gateway_provider == "xray_file"
        )
        if marzban_runtime_selected:
            normalized_base_url = self.marzban_base_url.strip()
            if not normalized_base_url:
                raise ValueError("MARZBAN_BASE_URL must not be blank when Marzban is selected")
            try:
                parsed_base_url = urlsplit(normalized_base_url)
            except ValueError as exc:
                raise ValueError(
                    "MARZBAN_BASE_URL must be a valid URL when Marzban is selected"
                ) from exc
            if not parsed_base_url.scheme or not parsed_base_url.netloc:
                raise ValueError(
                    "MARZBAN_BASE_URL must include a URL scheme and host when Marzban is selected"
                )
            if parsed_base_url.username is not None or parsed_base_url.password is not None:
                raise ValueError("MARZBAN_BASE_URL must not contain embedded credentials")
            if parsed_base_url.fragment:
                raise ValueError("MARZBAN_BASE_URL must not contain a URL fragment")
            if not self.marzban_admin_username.strip():
                raise ValueError(
                    "MARZBAN_ADMIN_USERNAME must not be blank when Marzban is selected"
                )
            if not self.marzban_admin_password:
                raise ValueError(
                    "MARZBAN_ADMIN_PASSWORD must not be blank when Marzban is selected"
                )
            if self.marzban_verify_tls and not self.marzban_ca_cert_path.strip():
                raise ValueError(
                    "MARZBAN_CA_CERT_PATH must not be blank when Marzban TLS "
                    "verification is enabled"
                )
            if self.app_env == "production":
                if parsed_base_url.scheme.lower() != "https":
                    raise ValueError("Production MARZBAN_BASE_URL must use verified HTTPS")
                if "example.invalid" in self.marzban_base_url:
                    raise ValueError("Production MARZBAN_BASE_URL must be configured")
                if "CHANGE_ME" in {self.marzban_admin_username, self.marzban_admin_password}:
                    raise ValueError("Production Marzban credentials must be configured")
                if not self.marzban_verify_tls:
                    raise ValueError("Production MARZBAN_VERIFY_TLS must remain enabled")

@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()

