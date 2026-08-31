from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.models import Secret


class SecretStoreError(RuntimeError):
    pass


def _cipher() -> Fernet:
    try:
        return Fernet(get_settings().secret_encryption_key.encode())
    except (TypeError, ValueError) as exc:
        raise SecretStoreError("SECRET_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        raise SecretStoreError("Secret value must not be empty")
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SecretStoreError("Secret cannot be decrypted with the configured key") from exc


def put_secret(db: Session, secret_ref: str, plaintext: str, purpose: str) -> Secret:
    existing = db.scalar(select(Secret).where(Secret.secret_ref == secret_ref))
    if existing is not None:
        if decrypt_secret(existing.ciphertext) == plaintext and existing.purpose == purpose:
            return existing
        existing.ciphertext = encrypt_secret(plaintext)
        existing.purpose = purpose
        db.flush()
        return existing

    secret = Secret(
        secret_ref=secret_ref,
        ciphertext=encrypt_secret(plaintext),
        purpose=purpose,
    )
    try:
        with db.begin_nested():
            db.add(secret)
            db.flush()
        return secret
    except IntegrityError:
        recovered = db.scalar(select(Secret).where(Secret.secret_ref == secret_ref))
        if recovered is None:
            raise
        recovered.ciphertext = encrypt_secret(plaintext)
        recovered.purpose = purpose
        db.flush()
        return recovered


def reveal_secret(db: Session, secret_ref: str) -> str:
    stored = db.scalar(select(Secret).where(Secret.secret_ref == secret_ref))
    if stored is None:
        raise SecretStoreError(f"Secret reference not found: {secret_ref}")
    return decrypt_secret(stored.ciphertext)
