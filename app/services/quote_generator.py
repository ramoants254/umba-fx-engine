"""Quote generation service."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict

import structlog

from app.config import settings
from app.database import get_pool
from app.services.rate_provider import RateProvider
from app.services.rate_router import RateRouter

log = structlog.get_logger()

CURRENCY_QUANTUM: Dict[str, Decimal] = {
    "USD": Decimal("0.01"),
    "EUR": Decimal("0.01"),
    "KES": Decimal("0.01"),
    "NGN": Decimal("0.01"),
}

SUPPORTED_CURRENCIES = set(CURRENCY_QUANTUM.keys())


class QuoteGenerator:
    """Handles logic for generating and persisting FX quotes."""

    def __init__(self, rate_provider: RateProvider) -> None:
        """Initialize generator with rate provider and router."""
        self.rates = rate_provider
        self.router = RateRouter(rate_provider)

    async def generate(
        self,
        customer_id: uuid.UUID,
        from_ccy: str,
        to_ccy: str,
        amount: Decimal,
    ) -> Dict[str, Any]:
        """Generate and insert an FX quote."""
        if from_ccy not in SUPPORTED_CURRENCIES or to_ccy not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {from_ccy} or {to_ccy}")
        if from_ccy == to_ccy:
            raise ValueError("from and to currencies must differ")
        if amount <= 0:
            raise ValueError("amount must be positive")

        if not self.rates.is_usable():
            raise RuntimeError("rates_unavailable")

        rate = self.router.effective_rate(from_ccy, to_ccy)
        dest_quantum = CURRENCY_QUANTUM[to_ccy]
        dest_amount = (amount * rate).quantize(dest_quantum, rounding=ROUND_HALF_UP)

        quote_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=settings.quote_ttl_seconds)

        pool = await get_pool()
        async with pool.acquire() as conn:
            customer = await conn.fetchrow(
                "SELECT id FROM customers WHERE id = $1", customer_id
            )
            if customer is None:
                raise ValueError("customer_not_found")

            await conn.execute(
                """
                INSERT INTO quotes
                    (id, customer_id, from_currency, to_currency,
                     source_amount, rate, dest_amount, status,
                     created_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDING', $8, $9)
                """,
                quote_id,
                customer_id,
                from_ccy,
                to_ccy,
                amount,
                rate,
                dest_amount,
                now,
                expires_at,
            )

        log.info(
            "quote_generated",
            quote_id=str(quote_id),
            customer_id=str(customer_id),
            pair=f"{from_ccy}/{to_ccy}",
            source_amount=str(amount),
            rate=str(rate),
            dest_amount=str(dest_amount),
        )

        return {
            "quote_id": quote_id,
            "customer_id": customer_id,
            "from_currency": from_ccy,
            "to_currency": to_ccy,
            "source_amount": str(amount),
            "rate": str(rate),
            "dest_amount": str(dest_amount),
            "created_at": now,
            "expires_at": expires_at,
        }
