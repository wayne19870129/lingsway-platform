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
