"""Schemas for public authentication and catalog endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CustomerCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    nickname: str | None = Field(default=None, max_length=80)
    source: str = Field(default="WEBSITE", max_length=32)


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_no: str
    email: str
    nickname: str | None
    status: str
    role: str


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_code: str
    name: str
    traffic_limit_bytes: int
    duration_days: int
    price: Decimal
    currency: str
    route_group_code: str
    status: str


class OrderCreate(BaseModel):
    plan_id: int
    client_request_id: str = Field(min_length=8, max_length=80)


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    customer_id: int
    plan_id: int
    amount: Decimal
    currency: str
    status: str
    payment_status: str
    created_at: datetime


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subscription_no: str
    customer_id: int
    plan_id: int
    status: str
    route_group_code: str
    traffic_quota_bytes: int
    traffic_used_bytes: int
    current_period_end: datetime | None
    service_expire_at: datetime


class SubscriptionDetails(BaseModel):
    id: int
    subscription_no: str
    status: str
    plan_name: str
    traffic_quota_bytes: int
    traffic_used_bytes: int
    service_expire_at: datetime
    subscription_url: str | None
    egress_ip: str | None
    egress_location: str | None
    egress_isp: str | None
    egress_ip_type: str | None


class BillingOrderRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=80)


class SubscriptionTokenResponse(BaseModel):
    subscription_url: str
    token_displayed_once: bool = True
