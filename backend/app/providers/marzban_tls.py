"""Shared Marzban internal TLS trust construction.

This module is deliberately Marzban-specific: accounting and gateway
providers share only this Settings-derived TLS contract, never clients,
tokens, or mutable operation state.
"""

from __future__ import annotations

import ssl
from pathlib import Path


class MarzbanTlsConfigurationError(ValueError):
    """Raised when explicit internal Marzban trust cannot be configured."""


def build_marzban_tls_verify(
    *, verify_tls: bool, ca_cert_path: str
) -> ssl.SSLContext | bool:
    """Return an explicit trust context, or disabled verification for tests.

    Production settings reject disabled verification.  The false branch is
    retained only so the existing provider contract can be exercised in
    explicitly non-production test configurations; it never falls back to
    system CAs.
    """

    if not verify_tls:
        return False
    normalized_path = ca_cert_path.strip()
    if not normalized_path:
        raise MarzbanTlsConfigurationError(
            "MARZBAN_CA_CERT_PATH must not be blank when Marzban TLS verification is enabled"
        )
    try:
        return ssl.create_default_context(cafile=str(Path(normalized_path)))
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise MarzbanTlsConfigurationError(
            "configured Marzban CA certificate could not be loaded"
        ) from exc
