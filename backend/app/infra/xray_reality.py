"""Explicit create-once provisioning for the repository-owned Xray Reality identity."""

from __future__ import annotations

import json
import secrets
import subprocess

from sqlalchemy.orm import Session

from backend.app.core.secrets import (
    SecretStoreError,
    get_or_create_secret,
    reveal_secret_for_purpose,
)
from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    XrayRealityConfig,
)

REALITY_IDENTITY_SECRET_REF = "gateway/xray/reality-identity"
REALITY_IDENTITY_PURPOSE = "XRAY_REALITY_IDENTITY"


class XrayRealityBootstrapError(RuntimeError):
    """Raised when the independent Reality identity provisioning operation fails."""


def _x25519_private_key(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "x25519"], check=False, capture_output=True, text=True
        )
    except OSError:
        raise XrayRealityBootstrapError("XRAY_REALITY_KEY_GENERATION_FAILED") from None
    if result.returncode != 0:
        raise XrayRealityBootstrapError("XRAY_REALITY_KEY_GENERATION_FAILED")
    for line in (result.stdout + result.stderr).splitlines():
        label, separator, value = line.partition(":")
        normalized_label = "".join(label.lower().split())
        if separator and normalized_label == "privatekey" and value.strip():
            return value.strip()
    raise XrayRealityBootstrapError("XRAY_REALITY_KEY_GENERATION_FAILED")


def _new_reality_identity(binary: str) -> str:
    return json.dumps(
        {
            "privateKey": _x25519_private_key(binary),
            "shortIds": [secrets.token_hex(8)],
        },
        separators=(",", ":"),
    )


def _parse_reality_identity(value: str) -> XrayRealityConfig:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise XrayRealityBootstrapError("XRAY_REALITY_IDENTITY_MALFORMED") from None
    if not isinstance(payload, dict) or set(payload) != {"privateKey", "shortIds"}:
        raise XrayRealityBootstrapError("XRAY_REALITY_IDENTITY_MALFORMED")
    try:
        return XrayRealityConfig(payload["privateKey"], payload["shortIds"])
    except (KeyError, TypeError, XrayCompositionError):
        raise XrayRealityBootstrapError("XRAY_REALITY_IDENTITY_MALFORMED") from None


def bootstrap_reality_identity(
    db: Session, *, xray_binary: str
) -> XrayRealityConfig:
    """Create the identity once, then read the canonical winner.

    This is intentionally separate from provider rendering.  Only the insert
    winner commits; an existing, malformed, wrong-purpose, or unreadable row
    is never replaced by a newly generated identity.
    """

    try:
        _, created = get_or_create_secret(
            db,
            REALITY_IDENTITY_SECRET_REF,
            lambda: _new_reality_identity(xray_binary),
            REALITY_IDENTITY_PURPOSE,
        )
        if created:
            db.commit()
        return _parse_reality_identity(
            reveal_secret_for_purpose(
                db,
                REALITY_IDENTITY_SECRET_REF,
                REALITY_IDENTITY_PURPOSE,
            )
        )
    except XrayRealityBootstrapError:
        raise
    except SecretStoreError:
        raise XrayRealityBootstrapError("XRAY_REALITY_IDENTITY_UNREADABLE") from None
    except Exception:
        raise XrayRealityBootstrapError("XRAY_REALITY_BOOTSTRAP_FAILED") from None

