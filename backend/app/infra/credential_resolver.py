"""Operation-scoped credential resolution for provider boundaries."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from backend.app.core.secrets import (
    SecretStoreError,
    decrypt_secret,
    reveal_secret_snapshot_for_purpose,
)
from backend.app.models import Secret
from backend.app.providers.base import (
    CredentialDTO,
    CredentialResolutionError,
    EgressCredentialSnapshot,
)
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretResolver,
    ControllerSecretSnapshot,
)
from backend.app.providers.gateway.xray_composition import (
    XrayRealityConfig,
    XrayRenderResolver,
)

MIHOMO_CONTROLLER_SECRET_PURPOSE = "MIHOMO_CONTROLLER_API_SECRET"
MIHOMO_CONTROLLER_SECRET_REF = "mihomo/api-secret"


class SqlMihomoControllerSecretResolver(ControllerSecretResolver):
    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve(self, secret_ref: str) -> ControllerSecretSnapshot:
        if not isinstance(secret_ref, str) or not secret_ref.strip():
            raise CredentialResolutionError("CREDENTIAL_REF_INVALID")
        try:
            snapshot = reveal_secret_snapshot_for_purpose(
                self._db, secret_ref, MIHOMO_CONTROLLER_SECRET_PURPOSE
            )
        except (SecretStoreError, ValueError):
            raise CredentialResolutionError("CREDENTIAL_RESOLUTION_FAILED") from None
        if not snapshot.value.strip() or snapshot.revision <= 0:
            raise CredentialResolutionError("CREDENTIAL_RESOLUTION_FAILED")
        return ControllerSecretSnapshot(secret_ref, snapshot.revision, snapshot.value)


def _current_secret_statement(
    secret_ref: str, *, purpose: str | None = None
) -> Select[tuple[Secret]]:
    """Build the locking, identity-map-refreshing current-read statement."""
    statement = (
        select(Secret)
        .where(Secret.secret_ref == secret_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if purpose is not None:
        statement = statement.where(Secret.purpose == purpose)
    return statement


class SqlAlchemyCredentialResolver(XrayRenderResolver):
    """Resolve one credential using the caller's provisioning Session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve(self, secret_ref: str) -> CredentialDTO:
        return self.resolve_egress_credential(secret_ref).credential

    def resolve_egress_credential(self, secret_ref: str) -> EgressCredentialSnapshot:
        if not isinstance(secret_ref, str) or not secret_ref.strip():
            raise CredentialResolutionError("CREDENTIAL_REF_INVALID")

        try:
            stored = self._db.scalar(_current_secret_statement(secret_ref))
            if stored is None:
                raise CredentialResolutionError("CREDENTIAL_NOT_FOUND")
            if (
                isinstance(stored.revision, bool)
                or not isinstance(stored.revision, int)
                or stored.revision <= 0
            ):
                raise CredentialResolutionError("CREDENTIAL_MALFORMED")

            payload = json.loads(decrypt_secret(stored.ciphertext))
            if not isinstance(payload, dict) or set(payload) != {"username", "password"}:
                raise CredentialResolutionError("CREDENTIAL_MALFORMED")
            username = payload["username"]
            password = payload["password"]
            if (
                not isinstance(username, str)
                or not username.strip()
                or not isinstance(password, str)
                or not password.strip()
            ):
                raise CredentialResolutionError("CREDENTIAL_MALFORMED")
            return EgressCredentialSnapshot(
                secret_ref,
                stored.revision,
                CredentialDTO(username=username, password=password),
            )
        except CredentialResolutionError:
            raise
        except SecretStoreError:
            raise CredentialResolutionError("CREDENTIAL_DECRYPT_FAILED") from None
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
            raise CredentialResolutionError("CREDENTIAL_MALFORMED") from None
        except Exception:
            raise CredentialResolutionError("CREDENTIAL_RESOLUTION_FAILED") from None

    def resolve_reality_identity(self) -> XrayRealityConfig:
        """Read the existing purpose-bound identity in this operation Session."""

        secret_ref = "gateway/xray/reality-identity"
        purpose = "XRAY_REALITY_IDENTITY"
        try:
            stored = self._db.scalar(_current_secret_statement(secret_ref, purpose=purpose))
            if stored is None:
                raise CredentialResolutionError("CREDENTIAL_NOT_FOUND")
            payload = json.loads(decrypt_secret(stored.ciphertext))
            if not isinstance(payload, dict) or set(payload) != {"privateKey", "shortIds"}:
                raise CredentialResolutionError("CREDENTIAL_MALFORMED")
            if (
                not isinstance(payload["privateKey"], str)
                or not payload["privateKey"].strip()
                or not isinstance(payload["shortIds"], list)
                or not payload["shortIds"]
                or any(
                    not isinstance(short_id, str) or not short_id.strip()
                    for short_id in payload["shortIds"]
                )
            ):
                raise CredentialResolutionError("CREDENTIAL_MALFORMED")
            return XrayRealityConfig(payload["privateKey"], payload["shortIds"])
        except CredentialResolutionError:
            raise
        except SecretStoreError:
            raise CredentialResolutionError("CREDENTIAL_DECRYPT_FAILED") from None
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
            raise CredentialResolutionError("CREDENTIAL_MALFORMED") from None
        except Exception:
            raise CredentialResolutionError("CREDENTIAL_RESOLUTION_FAILED") from None
