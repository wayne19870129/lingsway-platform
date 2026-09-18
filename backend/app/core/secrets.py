from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.models import Secret


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    value: str = field(repr=False)
    revision: int


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
    existing = db.scalar(
        select(Secret).where(Secret.secret_ref == secret_ref).with_for_update()
    )
    if existing is not None:
        if decrypt_secret(existing.ciphertext) == plaintext and existing.purpose == purpose:
            return existing
        existing.ciphertext = encrypt_secret(plaintext)
        existing.purpose = purpose
        existing.revision += 1
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
        recovered.revision += 1
        db.flush()
        return recovered


def get_or_create_secret(
    db: Session,
    secret_ref: str,
    plaintext_factory: Callable[[], str],
    purpose: str,
) -> tuple[Secret, bool]:
    """Create a Secret exactly once without overwriting a concurrent winner.

    The normal ``put_secret`` path intentionally keeps update semantics for
    callers that own a value and want to replace it.  A generated identity has
    different semantics: after the first insert wins, every concurrent loser
    must read that winner and must never write its own candidate over it.
    """
    statement = (
        select(Secret)
        .where(Secret.secret_ref == secret_ref)
        .execution_options(populate_existing=True)
    )
    existing = db.scalar(statement)
    if existing is not None:
        return existing, False

    values = {
        "secret_ref": secret_ref,
        "ciphertext": encrypt_secret(plaintext_factory()),
        "purpose": purpose,
    }
    if db.get_bind().dialect.name == "mysql":
        # MySQL removes the savepoint on a duplicate-key error in this path;
        # INSERT IGNORE lets the loser wait for and then read the winner.
        result = cast(
            CursorResult[Any], db.execute(insert(Secret).values(values).prefix_with("IGNORE"))
        )
        winner = db.scalar(statement.with_for_update())
        if winner is None:
            raise SecretStoreError("Secret insert did not produce a readable row")
        return winner, result.rowcount == 1

    secret = Secret(**values)
    try:
        with db.begin_nested():
            db.add(secret)
            db.flush()
        return secret, True
    except IntegrityError:
        winner = db.scalar(statement)
        if winner is None:
            raise
        return winner, False


def reveal_secret_for_purpose(db: Session, secret_ref: str, purpose: str) -> str:
    """Decrypt a purpose-bound secret without exposing its value or ref in errors."""
    statement = (
        select(Secret)
        .where(Secret.secret_ref == secret_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stored = db.scalar(statement)
    if stored is None:
        raise SecretStoreError("Secret reference not found")
    if stored.purpose != purpose:
        raise SecretStoreError("Secret purpose mismatch")
    return decrypt_secret(stored.ciphertext)


def reveal_secret_snapshot_for_purpose(
    db: Session, secret_ref: str, purpose: str
) -> SecretSnapshot:
    """Read and decrypt one purpose-bound secret with its non-secret revision."""
    statement = (
        select(Secret)
        .where(Secret.secret_ref == secret_ref, Secret.purpose == purpose)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stored = db.scalar(statement)
    if stored is None:
        raise SecretStoreError("Secret reference or purpose not found")
    return SecretSnapshot(value=decrypt_secret(stored.ciphertext), revision=stored.revision)


def reveal_secret(db: Session, secret_ref: str) -> str:
    stored = db.scalar(select(Secret).where(Secret.secret_ref == secret_ref))
    if stored is None:
        raise SecretStoreError(f"Secret reference not found: {secret_ref}")
    return decrypt_secret(stored.ciphertext)
