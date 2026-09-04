"""Schemas for administrator operations."""

from __future__ import annotations

from pydantic import BaseModel

from backend.app.schemas.public import OrderRead, SubscriptionRead


class PaymentConfirmation(BaseModel):
    payment_reference: str


class ProvisionResult(BaseModel):
    order: OrderRead
    subscription: SubscriptionRead
    subscription_url: str | None
