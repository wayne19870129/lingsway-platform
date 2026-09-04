"""Customer subscription endpoints and the unversioned subscription feed."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.dependencies import CurrentCustomer, DbSession
from backend.app.domain.ordering import BillingOrderType
from backend.app.domain.subscription_render import render_subscription
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    EgressBinding,
    EgressEndpoint,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from backend.app.providers.registry import build_registry
from backend.app.schemas.public import (
    BillingOrderRequest,
    OrderRead,
    SubscriptionDetails,
    SubscriptionRead,
    SubscriptionTokenResponse,
)

router = APIRouter()
subscription_feed_router = APIRouter()


def _owned_subscription(
    db: DbSession, subscription_id: int, customer: CurrentCustomer
) -> Subscription:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None or subscription.customer_id != customer.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    return subscription


def _create_renewal_order(
    db: DbSession,
    customer: CurrentCustomer,
    subscription: Subscription,
    client_request_id: str,
) -> Order:
    """Create only the renewal order branch from the legacy billing behavior."""
    existing = db.scalar(
        select(Order).where(
            Order.customer_id == customer.id,
            Order.client_request_id == client_request_id,
        )
    )
    if existing is not None:
        return existing
    current_plan = db.get(Plan, subscription.plan_id)
    if current_plan is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan not found")

    order = Order(
        order_no=f"ORD-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(6).upper()}",
        customer_id=customer.id,
        plan_id=current_plan.id,
        target_subscription_id=subscription.id,
        client_request_id=client_request_id,
        order_type=BillingOrderType.RENEWAL.value,
        amount=current_plan.price,
        currency=current_plan.currency,
        status=OrderStatus.PENDING,
        payment_status=PaymentStatus.UNPAID,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.get("/subscriptions", response_model=list[SubscriptionRead])
def my_subscriptions(db: DbSession, customer: CurrentCustomer) -> list[Subscription]:
    return list(
        db.scalars(
            select(Subscription)
            .where(Subscription.customer_id == customer.id)
            .order_by(Subscription.id.desc())
        )
    )


@router.get("/subscriptions/{subscription_id}/details", response_model=SubscriptionDetails)
def subscription_details(
    subscription_id: int, db: DbSession, customer: CurrentCustomer
) -> dict[str, object]:
    subscription = _owned_subscription(db, subscription_id, customer)
    plan = db.get(Plan, subscription.plan_id)
    binding_row = db.execute(
        select(EgressBinding, EgressEndpoint)
        .join(EgressEndpoint, EgressEndpoint.id == EgressBinding.egress_id)
        .where(
            EgressBinding.subscription_id == subscription.id,
            EgressBinding.released_at.is_(None),
        )
    ).first()
    endpoint = binding_row[1] if binding_row else None
    subscription_url: str | None = None
    if subscription.token_hash:
        subscription_url = SqlAlchemyProvisioningState(
            db, subscription.id
        ).current_subscription_url()
    return {
        "id": subscription.id,
        "subscription_no": subscription.subscription_no,
        "status": subscription.status.value,
        "plan_name": plan.name if plan else "未知套餐",
        "traffic_quota_bytes": subscription.traffic_quota_bytes,
        "traffic_used_bytes": subscription.traffic_used_bytes,
        "service_expire_at": subscription.service_expire_at,
        "subscription_url": subscription_url,
        "egress_ip": endpoint.host if endpoint else None,
        "egress_location": endpoint.location if endpoint else None,
        "egress_isp": endpoint.isp if endpoint else None,
        "egress_ip_type": endpoint.ip_type if endpoint else None,
    }


@router.post("/subscriptions/{subscription_id}/renew", response_model=OrderRead)
def create_renewal_order(
    subscription_id: int,
    data: BillingOrderRequest,
    db: DbSession,
    customer: CurrentCustomer,
) -> Order:
    subscription = _owned_subscription(db, subscription_id, customer)
    return _create_renewal_order(db, customer, subscription, data.client_request_id)


@router.post(
    "/subscriptions/{subscription_id}/token/reset",
    response_model=SubscriptionTokenResponse,
)
def reset_token(
    subscription_id: int, db: DbSession, customer: CurrentCustomer
) -> SubscriptionTokenResponse:
    subscription = _owned_subscription(db, subscription_id, customer)
    raw_token = secrets.token_urlsafe(32)
    state = SqlAlchemyProvisioningState(db, subscription.id)
    state.store_subscription_token(str(customer.id), raw_token)
    db.commit()
    return SubscriptionTokenResponse(subscription_url=state.current_subscription_url())


@router.post(
    "/subscriptions/{subscription_id}/token/revoke", status_code=status.HTTP_204_NO_CONTENT
)
def revoke_token(subscription_id: int, db: DbSession, customer: CurrentCustomer) -> None:
    subscription = _owned_subscription(db, subscription_id, customer)
    subscription.token_hash = None
    subscription.token_revoked_at = datetime.now(UTC)
    db.commit()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@subscription_feed_router.get("/s/{token}")
def get_subscription(token: str, request: Request, db: DbSession) -> Response:
    token_hash = hashlib.sha256(token.replace("\\", "").encode()).hexdigest()
    subscription = db.scalar(
        select(Subscription).where(
            Subscription.token_hash == token_hash,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
        )
    )
    if subscription is None or not subscription.accounting_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    period = subscription.current_period
    used_bytes = max(0, int(period.used_bytes if period else 0))
    quota_bytes = max(0, int(period.quota_bytes if period else 0))
    expire_at = int(_utc(subscription.service_expire_at).timestamp())
    userinfo = f"upload=0; download={used_bytes}; total={quota_bytes}; expire={expire_at}"
    user_agent = request.headers.get("user-agent", "")
    links = build_registry(get_settings()).accounting.get_connection_links(
        subscription.accounting_user_id
    )
    rendered = render_subscription(
        links,
        user_agent,
        subscription_userinfo=userinfo,
    )
    etag = f'"{hashlib.sha256(rendered.content).hexdigest()}"'
    headers = {
        **rendered.headers,
        "ETag": etag,
        "Cache-Control": "private, no-store",
    }
    return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)
