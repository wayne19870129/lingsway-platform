import logging
from datetime import UTC, datetime

from backend.app.core.logging import CredentialRedactionFilter
from backend.app.infra.mihomo_materialization import VerifiedMaterialization


class CredentialRecord(logging.LogRecord):
    password: str


def test_known_credential_fields_are_redacted_before_formatting() -> None:
    record = CredentialRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        {"api_key": "aaaa11111111bbbb", "event": "safe"},
        (),
        None,
    )
    record.password = "left-secret-right"
    assert CredentialRedactionFilter().filter(record)
    assert record.msg == {"api_key": "aaaa****bbbb", "event": "safe"}
    assert record.password == "left****ight"
    rendered = logging.Formatter("%(message)s %(password)s").format(record)
    assert "aaaa11111111bbbb" not in rendered
    assert "left-secret-right" not in rendered


def test_mihomo_materialization_repr_is_secret_safe() -> None:
    value = VerifiedMaterialization(
        1, "provider", 2, "cache-token", "a" * 64, datetime.now(UTC), b"SENTINEL_YAML"
    )
    rendered = repr(value)
    assert "SENTINEL_YAML" not in rendered
    assert "cache-token" not in rendered
