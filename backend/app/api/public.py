"""Public authentication and catalog endpoints."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.security import hash_password, verify_password
from backend.app.dependencies import (
    CurrentAuth,
    CurrentCustomer,
    DbSession,
    issue_access_token,
    revoke_customer_sessions,
    revoke_session,
)
from backend.app.models import Customer, CustomerStatus, Plan, PlanStatus
from backend.app.schemas.public import (
    CustomerCreate,
    CustomerRead,
    LoginRequest,
    PasswordChange,
    PlanRead,
    TokenResponse,
)

router = APIRouter()


def public_number(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(6).upper()}"


@router.post("/auth/register", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
def register(data: CustomerCreate, request: Request, db: DbSession) -> Customer:
    del request
    customer = Customer(
        customer_no=public_number("CUS"),
        email=data.email.lower(),
        password_hash=hash_password(data.password),
        nickname=data.nickname,
        source=data.source,
    )
    db.add(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        ) from None
    db.refresh(customer)
    return customer


@router.post("/auth/login", response_model=TokenResponse)
def login(data: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    del request
    customer = db.scalar(select(Customer).where(Customer.email == data.email.lower()))
    if customer is None or not verify_password(data.password, customer.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if customer.status != CustomerStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive account")
    return TokenResponse(access_token=issue_access_token(db, customer))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(auth: CurrentAuth, db: DbSession) -> None:
    revoke_session(db, auth.session, "LOGOUT")


@router.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(data: PasswordChange, auth: CurrentAuth, db: DbSession) -> None:
    customer = db.scalar(
        select(Customer).where(Customer.id == auth.customer.id).with_for_update()
    )
    if customer is None or not verify_password(data.current_password, customer.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    customer.password_hash = hash_password(data.new_password)
    db.flush()
    revoke_customer_sessions(db, customer.id, "PASSWORD_CHANGED")


@router.get("/customers/me", response_model=CustomerRead)
def me(customer: CurrentCustomer) -> Customer:
    return customer


@router.get("/plans", response_model=list[PlanRead])
def list_plans(db: DbSession) -> list[Plan]:
    return list(
        db.scalars(select(Plan).where(Plan.status == PlanStatus.ACTIVE).order_by(Plan.price))
    )
