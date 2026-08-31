import logging

from backend.app.core.logging import CredentialRedactionFilter


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
