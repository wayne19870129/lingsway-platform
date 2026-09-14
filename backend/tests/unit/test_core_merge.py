import logging
from decimal import Decimal

from backend.app.core.config import Settings
from backend.app.core.logging import CredentialRedactionFilter, SubscriptionTokenFilter


def test_merged_settings_preserve_provider_and_legacy_fields() -> None:
    settings = Settings.from_env(
        {
            "EGRESS_PROVIDER": "mock",
            "GATEWAY_PROVIDER": "mock",
            "DATABASE_URL": "mysql+pymysql://example.invalid/test",
            "WEBSHARE_TOTAL_QUOTA_GB": "500",
        }
    )
    assert settings.egress_provider == "mock"
    assert settings.gateway_provider == "mock"
    assert settings.database_url == "mysql+pymysql://example.invalid/test"
    assert settings.webshare_total_quota_gb == Decimal("500")


def test_from_env_reads_xray_runtime_settings_for_phase_2c1() -> None:
    settings = Settings.from_env(
        {
            "GATEWAY_PROVIDER": "xray_file",
            "XRAY_CONFIG_PATH": "/srv/xray/config.json",
            "XRAY_BACKUP_DIR": "/srv/xray/backups",
            "XRAY_BINARY_PATH": "/srv/xray/bin/xray",
            "XRAY_ASSET_DIR": "/srv/xray/assets",
            "XRAY_RELOAD_COMMAND": "systemctl reload xray",
        }
    )
    assert settings.gateway_provider == "xray_file"
    assert settings.xray_config_path == "/srv/xray/config.json"
    assert settings.xray_backup_dir == "/srv/xray/backups"
    assert settings.xray_binary_path == "/srv/xray/bin/xray"
    assert settings.xray_asset_dir == "/srv/xray/assets"
    assert settings.xray_reload_command == "systemctl reload xray"


def test_default_settings_keep_gateway_provider_mock() -> None:
    """Default Settings() (no env at all) must not silently switch away
    from the mock gateway -- TASK-T16 Phase 2C1 acceptance criterion."""
    assert Settings().gateway_provider == "mock"


def test_merged_logging_redacts_credentials_and_subscription_tokens() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        {"token": "aaaa11111111bbbb", "url": "/s/customer-token"},
        (),
        None,
    )
    assert SubscriptionTokenFilter().filter(record)
    assert CredentialRedactionFilter().filter(record)
    assert record.msg == {
        "token": "aaaa****bbbb",
        "url": "/s/customer-token",
    }

    url_record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "GET /s/customer-token", (), None
    )
    assert SubscriptionTokenFilter().filter(url_record)
    assert url_record.msg != "GET /s/customer-token"
    assert str(url_record.msg).startswith("GET /s/<token_hash:")
