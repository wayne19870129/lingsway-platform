"""Schemas for public authentication and catalog endpoints."""

from __future__ import annotations

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
