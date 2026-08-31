"""Structured-log helpers that never expose complete credentials."""

import hashlib
import logging
import re
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
_SUBSCRIPTION_TOKEN = re.compile(r"/s/(?!<token_hash:)([^/?\s]+)")


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


def redact_subscription_tokens(value: object) -> object:
    if not isinstance(value, str):
        return value

    def replacement(match: re.Match[str]) -> str:
        digest_prefix = hashlib.sha256(match.group(1).encode()).hexdigest()[:8]
        return f"/s/<token_hash:{digest_prefix}>"

    return _SUBSCRIPTION_TOKEN.sub(replacement, value)


class SubscriptionTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_subscription_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_subscription_tokens(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_subscription_tokens(value) for key, value in record.args.items()
            }
        return True


def configure_sensitive_log_redaction() -> None:
    current_factory = logging.getLogRecordFactory()
    if not getattr(current_factory, "_subscription_token_redacting", False):

        def redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = current_factory(*args, **kwargs)
            SubscriptionTokenFilter().filter(record)
            CredentialRedactionFilter().filter(record)
            return record

        redacting_factory._subscription_token_redacting = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(redacting_factory)

    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SubscriptionTokenFilter) for item in logger.filters):
        logger.addFilter(SubscriptionTokenFilter())
    if not any(isinstance(item, CredentialRedactionFilter) for item in logger.filters):
        logger.addFilter(CredentialRedactionFilter())
