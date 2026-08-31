import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from backend.app.core.config import get_settings

PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    salt_encoded = base64.urlsafe_b64encode(salt).decode()
    digest_encoded = base64.urlsafe_b64encode(digest).decode()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_encoded}${digest_encoded}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_access_token(
    customer_id: int, role: str, jti: str | None = None, minutes: int = 60
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(customer_id),
        "role": role,
        "jti": jti or secrets.token_urlsafe(32),
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, object]:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
