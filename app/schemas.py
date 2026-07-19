"""Pydantic request/response schemas for the FX engine API."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── Customers ────────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    """Request body for creating a customer."""
    name: str = Field(..., min_length=1, max_length=255)


class CustomerResponse(BaseModel):
    """Response for a created/fetched customer."""
    id: UUID
    name: str
    created_at: datetime


class BalanceResponse(BaseModel):
    """Single currency balance."""
    currency: str
    balance: str  # string to preserve decimal precision


class CustomerBalancesResponse(BaseModel):
    """All balances for a customer."""
    customer_id: UUID
    balances: list[BalanceResponse]


class CreditRequest(BaseModel):
    """Request body for crediting a customer balance."""
    currency: str = Field(..., pattern=r"^(USD|EUR|KES|NGN)$")
    amount: Decimal = Field(..., gt=0)

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_to_decimal(cls, v: object) -> Decimal:
        """Accept string or numeric input, always produce Decimal."""
        return Decimal(str(v))


class CreditResponse(BaseModel):
    """Response after crediting a balance."""
    customer_id: UUID
    currency: str
    new_balance: str


# ── Quotes ───────────────────────────────────────────────────────────────────

class QuoteRequest(BaseModel):
    """Request body for generating an FX quote."""
    customer_id: UUID
    from_currency: str = Field(..., pattern=r"^(USD|EUR|KES|NGN)$")
    to_currency: str = Field(..., pattern=r"^(USD|EUR|KES|NGN)$")
    amount: Decimal = Field(..., gt=0)

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_to_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class QuoteResponse(BaseModel):
    """Response for a generated quote."""
    quote_id: UUID
    customer_id: UUID
    from_currency: str
    to_currency: str
    source_amount: str
    rate: str
    dest_amount: str
    created_at: datetime
    expires_at: datetime


# ── Execution ────────────────────────────────────────────────────────────────

class ExecuteResponse(BaseModel):
    """Response for an executed quote."""
    transaction_id: UUID
    quote_id: UUID
    customer_id: UUID
    from_currency: str
    to_currency: str
    source_amount: str
    dest_amount: str
    rate: str
    executed_at: datetime


# ── Rates ────────────────────────────────────────────────────────────────────

class RatePair(BaseModel):
    """Buy/sell rates for a currency pair."""
    pair: str
    buy: str
    sell: str


class RatesSnapshot(BaseModel):
    """Full snapshot of current rates."""
    rates: list[RatePair]
    last_updated: datetime | None
    is_stale: bool


# ── Health & Metrics ─────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    db: str
    rates_status: str
    rates_age_seconds: float | None


class MetricsResponse(BaseModel):
    """Observability metrics."""
    quotes_generated: int
    quotes_executed: int
    execution_errors: int
    rate_fetch_successes: int
    rate_fetch_failures: int
    rate_age_seconds: float | None


# ── Errors ───────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: str | None = None
    correlation_id: str | None = None
