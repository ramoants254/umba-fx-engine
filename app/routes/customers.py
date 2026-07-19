"""Customer management routes."""
from __future__ import annotations

import uuid
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import structlog

from app.database import get_pool
from app.schemas import (
    CustomerCreate,
    CustomerResponse,
    CustomerBalancesResponse,
    BalanceResponse,
    CreditRequest,
    CreditResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/customers", tags=["customers"])

SUPPORTED_CURRENCIES = ["USD", "EUR", "KES", "NGN"]


@router.post("", status_code=201, response_model=CustomerResponse)
async def create_customer(body: CustomerCreate, request: Request):
    """Create a new customer with zero balances in all currencies."""
    cid = request.state.correlation_id
    customer_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO customers (id, name, created_at) VALUES ($1, $2, $3)",
                customer_id,
                body.name,
                now,
            )
            # Initialise zero balances for all supported currencies
            for ccy in SUPPORTED_CURRENCIES:
                await conn.execute(
                    """
                    INSERT INTO balances (customer_id, currency, balance, updated_at)
                    VALUES ($1, $2, 0, $3)
                    """,
                    customer_id,
                    ccy,
                    now,
                )

    log.info("customer_created", customer_id=str(customer_id), correlation_id=cid)
    return CustomerResponse(id=customer_id, name=body.name, created_at=now)


@router.get("/{customer_id}/balances", response_model=CustomerBalancesResponse)
async def get_balances(customer_id: uuid.UUID, request: Request):
    """Return all currency balances for a customer."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify customer exists
        customer = await conn.fetchrow(
            "SELECT id FROM customers WHERE id = $1", customer_id
        )
        if customer is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "customer_not_found",
                    "correlation_id": request.state.correlation_id,
                },
            )

        rows = await conn.fetch(
            """
            SELECT currency, balance FROM balances
            WHERE customer_id = $1
            ORDER BY currency
            """,
            customer_id,
        )

    balances = [
        BalanceResponse(currency=row["currency"], balance=str(row["balance"]))
        for row in rows
    ]
    return CustomerBalancesResponse(customer_id=customer_id, balances=balances)


@router.post("/{customer_id}/balances/credit", response_model=CreditResponse)
async def credit_balance(
    customer_id: uuid.UUID, body: CreditRequest, request: Request
):
    """Credit (add to) a customer's balance in a specific currency.

    This is a test-fixture endpoint for seeding balances.
    """
    cid = request.state.correlation_id
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the balance row
            row = await conn.fetchrow(
                """
                SELECT balance FROM balances
                WHERE customer_id = $1 AND currency = $2
                FOR UPDATE
                """,
                customer_id,
                body.currency,
            )
            if row is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "customer_not_found",
                        "correlation_id": cid,
                    },
                )

            now = datetime.now(timezone.utc)
            new_balance = row["balance"] + body.amount
            await conn.execute(
                """
                UPDATE balances
                SET balance = $1, updated_at = $2
                WHERE customer_id = $3 AND currency = $4
                """,
                new_balance,
                now,
                customer_id,
                body.currency,
            )

    log.info(
        "balance_credited",
        customer_id=str(customer_id),
        currency=body.currency,
        amount=str(body.amount),
        new_balance=str(new_balance),
        correlation_id=cid,
    )
    return CreditResponse(
        customer_id=customer_id,
        currency=body.currency,
        new_balance=str(new_balance),
    )
