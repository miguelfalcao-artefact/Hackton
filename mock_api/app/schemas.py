"""Pydantic request and response models — this is the published contract."""
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- errors
class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# ----------------------------------------------------------------- pagination
class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    has_next: bool


# ------------------------------------------------------------------- products
class ProductOut(ORMModel):
    id: str
    sku: str
    name: str
    category: str
    price_cents: int
    currency: str
    stock: int
    active: bool
    created_at: datetime


# ------------------------------------------------------------------ customers
class CustomerCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=160)
    country: str = Field(default="BR", min_length=2, max_length=2)
    segment: str = Field(default="new", pattern="^(new|regular|vip)$")


class CustomerOut(ORMModel):
    id: str
    email: str
    name: str
    country: str
    segment: str
    created_at: datetime


# --------------------------------------------------------------------- orders
class OrderItemIn(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, le=100)


class OrderCreate(BaseModel):
    customer_id: str
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    channel: str = Field(default="web", max_length=32)


class OrderItemOut(ORMModel):
    id: str
    product_id: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int


class OrderOut(ORMModel):
    id: str
    customer_id: str
    status: str
    total_cents: int
    currency: str
    channel: str
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)


class PaymentRequest(BaseModel):
    # Any token starting with "tok_decline" is refused, deterministically. It is
    # the handle the traffic generator uses to produce 402s on purpose.
    payment_token: str = Field(default="tok_ok", max_length=64)
    method: str = Field(default="card", pattern="^(card|pix|boleto)$")


class ReasonRequest(BaseModel):
    reason: str = Field(default="unspecified", max_length=200)


# --------------------------------------------------------------------- health
class HealthOut(BaseModel):
    status: str
    service: str
    version: str


class ReadyOut(BaseModel):
    status: str
    db: str
