"""Structured-log helpers that never expose complete credentials."""

import logging
from collections.abc import Mapping

SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "jwt_secret",
        "password",
        "secret",
        "token",
        "webshare_api_key",
    }
)


def redact_secret(value: object) -> str:
    text = str(value)
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}****{text[-4:]}"


def redact_mapping(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: redact_secret(value) if key.lower() in SENSITIVE_FIELDS else value
        for key, value in values.items()
    }


class CredentialRedactionFilter(logging.Filter):
    """Redact known structured fields before a formatter sees the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in SENSITIVE_FIELDS:
            if hasattr(record, key):
                setattr(record, key, redact_secret(getattr(record, key)))
        if isinstance(record.msg, Mapping):
            record.msg = redact_mapping(record.msg)
        return True
