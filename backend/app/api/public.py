"""Public authentication and catalog endpoints."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.catalog import SELLABLE_PLAN_CODES
from backend.app.core.security import hash_password, verify_password
from backend.app.dependencies import (
    CurrentAuth,
    CurrentCustomer,
    DbSession,
    ManagedRegistry,
    issue_access_token,
    revoke_customer_sessions,
    revoke_session,
)
from backend.app.domain.capacity import CapacityExceededError, ensure_capacity
from backend.app.domain.ordering import BillingOrderType
from backend.app.models import (
    AuditLog,
    Customer,
    CustomerStatus,
    EgressEndpoint,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    PlanStatus,
)
from backend.app.providers.base import NotifyEvent
from backend.app.schemas.public import (
    CustomerCreate,
    CustomerRead,
    LoginRequest,
    OrderCreate,
    OrderRead,
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
    """Only the catalogue's sellable tiers (TASK-S06).

    This used to return every ACTIVE plan and leave filtering to each
    frontend page, which meant the sellable list existed in three places
    and could drift. Narrowing it here makes the backend the single
    authority: a plan row that is ACTIVE but outside the catalogue (a
    legacy tier, a migration artefact) is never offered for purchase.
    """
    return list(
        db.scalars(
            select(Plan)
            .where(
                Plan.status == PlanStatus.ACTIVE,
                Plan.plan_code.in_(SELLABLE_PLAN_CODES),
            )
            .order_by(Plan.price)
        )
    )


@router.post("/orders", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
def place_order(
    data: OrderCreate, db: DbSession, customer: CurrentCustomer, registry: ManagedRegistry
) -> Order:
    existing = db.scalar(
        select(Order).where(
            Order.customer_id == customer.id,
            Order.client_request_id == data.client_request_id,
        )
    )
    if existing is not None:
        return existing

    plan = db.get(Plan, data.plan_id)
    if plan is None or plan.status != PlanStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Plan is not available")

    # The domain capacity guard is evaluated while creating the order.  The
    # customer-facing flow never marks payment as paid; any future payment
    # confirmation must repeat this guard immediately before that transition.
    try:
        capacity = registry.egress.capacity()
        ensure_capacity(capacity, Decimal(plan.traffic_limit_bytes) / Decimal(1024**3))
    except CapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    # Read-only precheck: each customer needs one dedicated IP, and GB quota
    # is not the real bottleneck (see TASK-T15). This mirrors the exact
    # filter used by the authoritative, lock-holding allocation at
    # confirm-payment time (SqlAlchemyProvisioningState.allocate_endpoint) so
    # a pass here can't diverge from what that step will actually allocate.
    # It does not reserve or lock a row -- the endpoint can still be taken by
    # another order before this one is paid, which confirm-payment re-checks.
    available_ip_count = db.scalar(
        select(func.count())
        .select_from(EgressEndpoint)
        .where(
            EgressEndpoint.status == "AVAILABLE",
            EgressEndpoint.purpose == "PRODUCTION",
            EgressEndpoint.capacity == 1,
            EgressEndpoint.current_count == 0,
        )
    )
    has_available_ip = (available_ip_count or 0) > 0
    if not has_available_ip:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No dedicated egress IP is currently available",
        )

    order = Order(
        order_no=public_number("ORD"),
        customer_id=customer.id,
        plan_id=plan.id,
        client_request_id=data.client_request_id,
        order_type=BillingOrderType.PURCHASE.value,
        amount=plan.price,
        currency=plan.currency,
        status=OrderStatus.PENDING,
        payment_status=PaymentStatus.UNPAID,
    )
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        recovered = db.scalar(
            select(Order).where(
                Order.customer_id == customer.id,
                Order.client_request_id == data.client_request_id,
            )
        )
        if recovered is None:
            raise
        return recovered
    db.refresh(order)
    return order


@router.get("/orders", response_model=list[OrderRead])
def my_orders(db: DbSession, customer: CurrentCustomer) -> list[Order]:
    return list(
        db.scalars(select(Order).where(Order.customer_id == customer.id).order_by(Order.id.desc()))
    )


@router.post("/orders/{order_id}/payment-notice")
def payment_notice(
    order_id: int,
    db: DbSession,
    customer: CurrentCustomer,
    registry: ManagedRegistry,
) -> dict[str, object]:
    order = db.scalar(
        select(Order).where(Order.id == order_id, Order.customer_id == customer.id)
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.status != OrderStatus.PENDING or order.payment_status != PaymentStatus.UNPAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order is not awaiting payment",
        )

    previous = db.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "ORDER_PAYMENT_NOTICE",
            AuditLog.entity_type == "ORDER",
            AuditLog.entity_id == str(order.id),
            AuditLog.result == "SUCCESS",
        )
        .order_by(AuditLog.id.desc())
    )
    if previous is not None:
        return {"status": "already_notified", "notified_at": previous.created_at.isoformat()}

    plan = db.get(Plan, order.plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order plan not found")
    event = NotifyEvent(
        "ORDER_PAYMENT_NOTICE",
        {
            "order_no": order.order_no,
            "plan": plan.name,
            "amount": str(order.amount),
            "currency": order.currency,
            "customer_email": customer.email,
        },
    )
    try:
        delivery = registry.notify.send(event)
        if not delivery.delivered:
            raise RuntimeError(delivery.error or "notification delivery failed")
    except Exception as exc:
        db.add(
            AuditLog(
                actor_customer_id=customer.id,
                action="ORDER_PAYMENT_NOTICE",
                entity_type="ORDER",
                entity_id=str(order.id),
                result="FAILED",
                detail=type(exc).__name__,
            )
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment notice notification failed",
        ) from None

    db.add(
        AuditLog(
            actor_customer_id=customer.id,
            action="ORDER_PAYMENT_NOTICE",
            entity_type="ORDER",
            entity_id=str(order.id),
            result="SUCCESS",
            detail="notification_sent",
        )
    )
    db.commit()
    return {"status": "notified"}
