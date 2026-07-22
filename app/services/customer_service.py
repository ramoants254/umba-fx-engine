"""Customer balance and profile service."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import structlog

from app.database import get_pool

log = structlog.get_logger()

SUPPORTED_CURRENCIES = ["USD", "EUR", "KES", "NGN"]


class CustomerService:
    """Handles business logic for customer profile and balance operations."""

    async def create_customer(self, name: str) -> Dict[str, Any]:
        """Create a new customer with zero balances in all currencies."""
        customer_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO customers (id, name, created_at) VALUES ($1, $2, $3)",
                    customer_id,
                    name,
                    now,
                )
                # Initialize zero balances for all supported currencies
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

        log.info("customer_created", customer_id=str(customer_id))
        return {"id": customer_id, "name": name, "created_at": now}

    async def get_balances(self, customer_id: uuid.UUID) -> List[Dict[str, Any]]:
        """Return all currency balances for a customer."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Verify customer exists
            customer = await conn.fetchrow(
                "SELECT id FROM customers WHERE id = $1", customer_id
            )
            if customer is None:
                raise ValueError("customer_not_found")

            rows = await conn.fetch(
                """
                SELECT currency, balance FROM balances
                WHERE customer_id = $1
                ORDER BY currency
                """,
                customer_id,
            )

        return [
            {"currency": row["currency"], "balance": str(row["balance"])}
            for row in rows
        ]

    async def credit_balance(
        self, customer_id: uuid.UUID, currency: str, amount: Decimal
    ) -> Dict[str, Any]:
        """Credit (add to) a customer's balance in a specific currency."""
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
                    currency,
                )
                if row is None:
                    raise ValueError("customer_not_found")

                now = datetime.now(timezone.utc)
                new_balance = row["balance"] + amount
                await conn.execute(
                    """
                    UPDATE balances
                    SET balance = $1, updated_at = $2
                    WHERE customer_id = $3 AND currency = $4
                    """,
                    new_balance,
                    now,
                    customer_id,
                    currency,
                )

        log.info(
            "balance_credited",
            customer_id=str(customer_id),
            currency=currency,
            amount=str(amount),
            new_balance=str(new_balance),
        )
        return {
            "customer_id": customer_id,
            "currency": currency,
            "new_balance": str(new_balance),
        }
