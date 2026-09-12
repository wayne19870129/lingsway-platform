"""Authentication dependencies and JWT-session lifecycle helpers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import create_access_token, decode_access_token
from backend.app.models import Customer, CustomerRole, CustomerStatus, JwtSession
from backend.app.providers.registry import ProviderRegistry

DbSession = Annotated[Session, Depends(get_db)]
bearer = HTTPBearer(auto_error=False)


class ProviderRegistryNotConfigured(RuntimeError):
    """Raised by :func:`get_provider_registry` when the application-scoped
    ``ProviderRegistry`` was never installed on ``app.state`` -- i.e. the
    FastAPI ``lifespan`` in ``main.py`` never ran for this app instance.

    TASK-T16 Phase 2B8 (independent-review Major 1): this dependency
    fails closed instead of silently constructing an unmanaged fallback
    registry that nothing would ever close. Every real ASGI server
    (uvicorn included) always runs the lifespan, so this is only ever
    reachable from a test that built ``TestClient(app)`` without the
    ``with`` context manager needed to trigger startup/shutdown events --
    such a test must either use ``with TestClient(app) as client:`` or
    override this dependency (``app.dependency_overrides``) explicitly.
    """


def get_provider_registry(request: Request) -> ProviderRegistry:
    """The one place route handlers reach the application-scoped
    ``ProviderRegistry`` -- built exactly once by ``main.py``'s
    ``lifespan`` and stored on ``app.state``, so every request within the
    same process reuses the same instance (and, once a real
    resource-owning provider is wired in, its resources) instead of each
    route constructing and discarding its own. Raises
    :class:`ProviderRegistryNotConfigured` (fail closed, never a silent
    unmanaged fallback) if the lifespan never ran.
    """
    registry: ProviderRegistry | None = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise ProviderRegistryNotConfigured(
            "app.state.provider_registry is not set -- the FastAPI lifespan "
            "never ran for this app instance"
        )
    return registry


ManagedRegistry = Annotated[ProviderRegistry, Depends(get_provider_registry)]


@dataclass(frozen=True, slots=True)
class AuthContext:
    customer: Customer
    session: JwtSession


def issue_access_token(db: Session, customer: Customer, minutes: int = 60) -> str:
    now = datetime.now(UTC)
    jti = secrets.token_urlsafe(32)
    token = create_access_token(customer.id, customer.role.value, jti=jti, minutes=minutes)
    db.add(
        JwtSession(
            jti=jti,
            customer_id=customer.id,
            issued_at=now,
            expires_at=now + timedelta(minutes=minutes),
        )
    )
    db.commit()
    return token


def revoke_session(db: Session, session: JwtSession, reason: str) -> None:
    if session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)
        session.revoke_reason = reason
        db.commit()


def revoke_customer_sessions(db: Session, customer_id: int, reason: str) -> int:
    sessions = list(
        db.scalars(
            select(JwtSession)
            .where(
                JwtSession.customer_id == customer_id,
                JwtSession.revoked_at.is_(None),
            )
            .with_for_update()
        )
    )
    now = datetime.now(UTC)
    for session in sessions:
        session.revoked_at = now
        session.revoke_reason = reason
    db.commit()
    return len(sessions)


def get_auth_context(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    try:
        payload = decode_access_token(credentials.credentials)
        customer_id = int(str(payload["sub"]))
        jti = str(payload["jti"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from None

    customer = db.get(Customer, customer_id)
    if customer is None or customer.status != CustomerStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Inactive account",
        )
    jwt_session = db.get(JwtSession, jti)
    expires_at = jwt_session.expires_at if jwt_session else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        jwt_session is None
        or jwt_session.customer_id != customer.id
        or jwt_session.revoked_at is not None
        or expires_at is None
        or expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")
    return AuthContext(customer=customer, session=jwt_session)


CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]


def get_current_customer(context: CurrentAuth) -> Customer:
    return context.customer


CurrentCustomer = Annotated[Customer, Depends(get_current_customer)]


def require_admin(customer: CurrentCustomer) -> Customer:
    if customer.role not in {CustomerRole.ADMIN, CustomerRole.SUPER_ADMIN}:
        raise HTTPException(status_code=403, detail="Admin role required")
    return customer


AdminCustomer = Annotated[Customer, Depends(require_admin)]
