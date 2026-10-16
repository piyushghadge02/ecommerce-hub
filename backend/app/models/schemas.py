"""Pydantic v2 request/response DTOs matching the API contract (§4).

Money is Decimal server-side. Datetimes serialize as ISO-8601.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

OrderStatus = Literal["PENDING", "PROCESSING", "SHIPPED"]
ESSyncStatus = Literal["synced", "pending_retry", "queued"]


# ---------------------------------------------------------------- users ---
class UserOut(BaseModel):
    id: int
    name: str
    email: str


# -------------------------------------------------------------- products ---
class ProductVariant(BaseModel):
    model_config = {"extra": "allow"}  # variants may carry extra attributes

    sku: str
    color: Optional[str] = None
    stock: Optional[int] = None


class ProductOut(BaseModel):
    id: str
    sku: str
    title: str
    description: Optional[str] = None
    price: float
    category: str
    tags: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    variants: List[ProductVariant] = Field(default_factory=list)
    active: bool = True
    updated_at: Optional[datetime] = None


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: Optional[str] = None
    price: float = Field(gt=0)
    category: str = Field(min_length=1)
    tags: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    variants: List[ProductVariant] = Field(min_length=1)
    active: bool = True


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    attributes: Optional[Dict[str, Any]] = None
    variants: Optional[List[ProductVariant]] = None
    active: Optional[bool] = None

    @field_validator("price")
    @classmethod
    def _price_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("price must be > 0")
        return v

    @field_validator("title", "sku")
    @classmethod
    def _non_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v


# ---------------------------------------------------------------- orders ---
class OrderItemCreate(BaseModel):
    product_id: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class OrderCreate(BaseModel):
    user_id: int
    items: List[OrderItemCreate] = Field(min_length=1)
    idempotency_key: Optional[str] = None


class OrderItemOut(BaseModel):
    product_id: str
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class ESSyncInfo(BaseModel):
    strategy: str
    status: ESSyncStatus


class OrderCreateOut(BaseModel):
    order_id: int
    user_id: int
    status: OrderStatus
    total_amount: Decimal
    order_date: datetime
    updated_at: datetime
    items: List[OrderItemOut]
    es_sync: ESSyncInfo


class OrderSyncInfo(BaseModel):
    es_in_sync: Optional[bool] = None
    pg_updated_at: datetime
    es_updated_at: Optional[datetime] = None


class OrderDetailOut(BaseModel):
    order_id: int
    user_id: int
    customer: UserOut
    status: OrderStatus
    total_amount: Decimal
    order_date: datetime
    updated_at: datetime
    items: List[OrderItemOut]
    sync: OrderSyncInfo


class StatusUpdate(BaseModel):
    status: OrderStatus


class StatusUpdateOut(BaseModel):
    order_id: int
    status: OrderStatus
    updated_at: datetime
    es_sync: ESSyncInfo


# ---------------------------------------------------------------- search ---
class SearchRequest(BaseModel):
    q: Optional[str] = None
    statuses: Optional[List[OrderStatus]] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    min_total: Optional[float] = None
    max_total: Optional[float] = None
    sort_by: Literal["order_date", "total_amount", "order_id"] = "order_date"
    sort_dir: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class SearchCustomer(BaseModel):
    id: int
    name: str
    email: str


class SearchOrderRow(BaseModel):
    order_id: int
    order_date: datetime
    customer: SearchCustomer
    total_amount: float
    status: OrderStatus


class SearchAggs(BaseModel):
    revenue: float
    by_status: Dict[str, int] = Field(default_factory=dict)
    total_orders: int


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    orders: List[SearchOrderRow]
    aggs: SearchAggs


# ----------------------------------------------------------------- admin ---
class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: Literal["up", "down"]
    mongodb: Literal["up", "down"]
    elasticsearch: Literal["up", "down"]
    sync_strategy: str


class SyncStatusOut(BaseModel):
    outbox_pending: int
    outbox_failed: int
    polling_watermark: Optional[str] = None
    elasticsearch_docs: Optional[int] = None


class ReindexOut(BaseModel):
    indexed: int
