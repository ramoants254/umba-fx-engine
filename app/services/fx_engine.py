"""FX engine: quote generation, execution, and rate routing.

All monetary calculations use Decimal. Rounding (ROUND_HALF_UP) is
applied only at the final converted amount. See SPEC.md §3 and §5.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

import structlog

from app.config import settings
from app.database import get_pool
from app.services.rate_provider import RateProvider

log = structlog.get_logger()

# Per-currency quantize steps (SPEC.md §3.2)
CURRENCY_QUANTUM: Dict[str, Decimal] = {
    "USD": Decimal("0.01"),
    "EUR": Decimal("0.01"),
    "KES": Decimal("0.01"),
    "NGN": Decimal("0.01"),
}

SUPPORTED_CURRENCIES = set(CURRENCY_QUANTUM.keys())


class FXEngine:
    """Core FX operations: quote generation and execution.

    This class contains no I/O-related imports (FastAPI, etc.).
    All database interaction uses asyncpg through the connection pool.
    """

    def __init__(self, rate_provider: RateProvider) -> None:
        self.rates = rate_provider
        self._quotes_generated: int = 0
        self._quotes_executed: int = 0
        self._execution_errors: int = 0

    # ── Metrics ──────────────────────────────────────────────────────────

    @property
    def quotes_generated(self) -> int:
        return self._quotes_generated

    @property
    def quotes_executed(self) -> int:
        return self._quotes_executed

    @property
    def execution_errors(self) -> int:
        return self._execution_errors

    # ── Quote Generation ─────────────────────────────────────────────────

    async def generate_quote(
        self,
        customer_id: uuid.UUID,
        from_ccy: str,
        to_ccy: str,
        amount: Decimal,
    ) -> Dict[str, Any]:
        """Generate an FX quote and persist it.

        Args:
            customer_id: UUID of the customer requesting the quote.
            from_ccy: Source currency code (USD, EUR, KES, NGN).
            to_ccy: Destination currency code.
            amount: Source amount to convert (must be positive).

        Returns:
            Dict with quote details including quote_id, rate, and expiry.

        Raises:
            ValueError: If currencies are invalid, amount is non-positive,
                or no rate is available.
            RuntimeError: If rates are stale/unavailable.
        """
        # Validation
        if from_ccy not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {from_ccy}")
        if to_ccy not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {to_ccy}")
        if from_ccy == to_ccy:
            raise ValueError("from and to currencies must differ")
        if amount <= 0:
            raise ValueError("amount must be positive")

        if not self.rates.is_usable():
            raise RuntimeError("rates_unavailable")

        # Resolve rate
        rate = self._effective_rate(from_ccy, to_ccy)

        # Calculate destination amount — round ONCE at the end
        dest_quantum = CURRENCY_QUANTUM[to_ccy]
        dest_amount = (amount * rate).quantize(dest_quantum, rounding=ROUND_HALF_UP)

        # Persist quote
        quote_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=settings.quote_ttl_seconds)

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Verify customer exists
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

        self._quotes_generated += 1
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

    # ── Execution ────────────────────────────────────────────────────────

    async def execute_quote(
        self,
        quote_id: uuid.UUID,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a pending quote: debit source, credit destination.

        The entire operation (idempotency check, balance updates, quote
        status change, transaction record) runs in a single PostgreSQL
        transaction with row-level locking via SELECT ... FOR UPDATE.

        Args:
            quote_id: UUID of the quote to execute.
            idempotency_key: Optional client-provided key for retry safety.

        Returns:
            Dict with transaction details.

        Raises:
            ValueError: If quote is not found, expired, already executed,
                or customer has insufficient balance.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Everything in one transaction
            async with conn.transaction():
                # ── Step 1: Lock and validate the quote ──────────────────
                quote = await conn.fetchrow(
                    """
                    SELECT id, customer_id, from_currency, to_currency,
                           source_amount, rate, dest_amount, status, expires_at
                    FROM quotes
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    quote_id,
                )

                if quote is None:
                    self._execution_errors += 1
                    raise ValueError("quote_not_found")

                # ── Step 1.5: Idempotency check (INSIDE the lock) ────────
                # Moving this after the SELECT FOR UPDATE ensures that concurrent
                # requests wait for the first request to finish and commit before checking.
                if idempotency_key:
                    existing = await conn.fetchrow(
                        "SELECT response FROM idempotency_keys WHERE key = $1",
                        idempotency_key,
                    )
                    if existing:
                        log.info(
                            "idempotent_replay",
                            idempotency_key=idempotency_key,
                            quote_id=str(quote_id),
                        )
                        return json.loads(existing["response"])

                if quote["status"] != "PENDING":
                    self._execution_errors += 1
                    raise ValueError("quote_already_executed")

                now = datetime.now(timezone.utc)
                if quote["expires_at"] < now:
                    # Mark as expired
                    await conn.execute(
                        "UPDATE quotes SET status = 'EXPIRED' WHERE id = $1",
                        quote_id,
                    )
                    self._execution_errors += 1
                    raise ValueError("quote_expired")

                customer_id = quote["customer_id"]
                from_ccy = quote["from_currency"]
                to_ccy = quote["to_currency"]
                source_amount = quote["source_amount"]
                dest_amount = quote["dest_amount"]
                rate = quote["rate"]

                # ── Step 2: Lock balance rows (alphabetical order) ───────
                currencies = sorted([from_ccy, to_ccy])
                for ccy in currencies:
                    balance_row = await conn.fetchrow(
                        """
                        SELECT balance FROM balances
                        WHERE customer_id = $1 AND currency = $2
                        FOR UPDATE
                        """,
                        customer_id,
                        ccy,
                    )
                    if balance_row is None:
                        self._execution_errors += 1
                        raise ValueError(
                            f"no balance record for {ccy}"
                        )

                # ── Step 3: Check sufficient source balance ──────────────
                source_balance = await conn.fetchval(
                    """
                    SELECT balance FROM balances
                    WHERE customer_id = $1 AND currency = $2
                    """,
                    customer_id,
                    from_ccy,
                )

                if source_balance < source_amount:
                    self._execution_errors += 1
                    raise ValueError("insufficient_balance")

                # ── Step 4: Debit source ─────────────────────────────────
                await conn.execute(
                    """
                    UPDATE balances
                    SET balance = balance - $1, updated_at = $2
                    WHERE customer_id = $3 AND currency = $4
                    """,
                    source_amount,
                    now,
                    customer_id,
                    from_ccy,
                )

                # ── Step 5: Credit destination ───────────────────────────
                await conn.execute(
                    """
                    UPDATE balances
                    SET balance = balance + $1, updated_at = $2
                    WHERE customer_id = $3 AND currency = $4
                    """,
                    dest_amount,
                    now,
                    customer_id,
                    to_ccy,
                )

                # ── Step 6: Mark quote as executed ───────────────────────
                await conn.execute(
                    """
                    UPDATE quotes
                    SET status = 'EXECUTED', executed_at = $1
                    WHERE id = $2
                    """,
                    now,
                    quote_id,
                )

                # ── Step 7: Insert transaction record ────────────────────
                tx_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO transactions
                        (id, quote_id, customer_id, from_currency,
                         to_currency, source_amount, dest_amount, rate,
                         executed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    tx_id,
                    quote_id,
                    customer_id,
                    from_ccy,
                    to_ccy,
                    source_amount,
                    dest_amount,
                    rate,
                    now,
                )

                # Build response
                response = {
                    "transaction_id": str(tx_id),
                    "quote_id": str(quote_id),
                    "customer_id": str(customer_id),
                    "from_currency": from_ccy,
                    "to_currency": to_ccy,
                    "source_amount": str(source_amount),
                    "dest_amount": str(dest_amount),
                    "rate": str(rate),
                    "executed_at": now.isoformat(),
                }

                # ── Step 8: Store idempotency key (inside txn) ───────────
                if idempotency_key:
                    await conn.execute(
                        """
                        INSERT INTO idempotency_keys (key, quote_id, response)
                        VALUES ($1, $2, $3)
                        """,
                        idempotency_key,
                        quote_id,
                        json.dumps(response),
                    )

        self._quotes_executed += 1
        log.info(
            "quote_executed",
            transaction_id=str(tx_id),
            quote_id=str(quote_id),
            customer_id=str(customer_id),
            pair=f"{from_ccy}/{to_ccy}",
            source_amount=str(source_amount),
            dest_amount=str(dest_amount),
        )

        return response

    # ── Rate Routing ─────────────────────────────────────────────────────

    def _effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Resolve the effective rate for a currency conversion.

        Resolution order (SPEC.md §2.3):
          1. Direct pair → sell rate
          2. Inverse pair → 1 / buy rate
          3. Cross via USD (two legs, spreads compound)
          4. Cross via EUR (two legs, spreads compound)
          5. Fail

        Args:
            from_ccy: Source currency code.
            to_ccy: Destination currency code.

        Returns:
            The effective Decimal rate.

        Raises:
            ValueError: If no rate path exists.
        """
        # 1. Direct lookup
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        # 2. Inverse lookup
        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            # 1/sell gives the worst rate for the customer (preserves spread)
            return Decimal("1") / inverse["sell"]

        # 3. Cross via USD
        rate = self._try_cross(from_ccy, to_ccy, "USD")
        if rate is not None:
            return rate

        # 4. Cross via EUR
        rate = self._try_cross(from_ccy, to_ccy, "EUR")
        if rate is not None:
            return rate

        raise ValueError(f"no rate available for {from_ccy}/{to_ccy}")

    def _try_cross(
        self, from_ccy: str, to_ccy: str, bridge: str
    ) -> Optional[Decimal]:
        """Attempt a two-leg cross rate through a bridge currency.

        Each leg resolves independently (direct buy or inverse 1/sell).
        Rates multiply, so spreads compound naturally.

        Returns None if either leg cannot be resolved.
        """
        if from_ccy == bridge or to_ccy == bridge:
            return None

        leg1 = self._resolve_single_leg(from_ccy, bridge)
        if leg1 is None:
            return None

        leg2 = self._resolve_single_leg(bridge, to_ccy)
        if leg2 is None:
            return None

        return leg1 * leg2

    def _resolve_single_leg(
        self, from_ccy: str, to_ccy: str
    ) -> Optional[Decimal]:
        """Resolve a single leg: direct buy or inverse 1/sell."""
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            return Decimal("1") / inverse["sell"]

        return None
