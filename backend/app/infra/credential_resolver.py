"""Operation-scoped credential resolution for provider boundaries."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from backend.app.core.secrets import SecretStoreError, decrypt_secret
from backend.app.models import Secret
from backend.app.providers.base import (
    CredentialDTO,
    CredentialResolutionError,
    CredentialResolver,
)


def _current_secret_statement(secret_ref: str) -> Select[tuple[Secret]]:
    """Build the locking, identity-map-refreshing current-read statement."""
    return (
        select(Secret)
        .where(Secret.secret_ref == secret_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class SqlAlchemyCredentialResolver(CredentialResolver):
    """Resolve one credential using the caller's provisioning Session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve(self, secret_ref: str) -> CredentialDTO:
        if not isinstance(secret_ref, str) or not secret_ref.strip():
            raise CredentialResolutionError("CREDENTIAL_REF_INVALID")

        try:
            stored = self._db.scalar(_current_secret_statement(secret_ref))
            if stored is None:
                raise CredentialResolutionError("CREDENTIAL_NOT_FOUND")

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
            return CredentialDTO(username=username, password=password)
        except CredentialResolutionError:
            raise
        except SecretStoreError:
            raise CredentialResolutionError("CREDENTIAL_DECRYPT_FAILED") from None
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
            raise CredentialResolutionError("CREDENTIAL_MALFORMED") from None
        except Exception:
            raise CredentialResolutionError("CREDENTIAL_RESOLUTION_FAILED") from None
