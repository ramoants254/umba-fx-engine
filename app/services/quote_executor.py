"""Quote execution service."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog

from app.database import get_pool

log = structlog.get_logger()


class QuoteExecutor:
    """Handles logic for executing FX quotes and updating balances atomically."""

    async def execute(
        self,
        quote_id: uuid.UUID,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a pending quote: debit source, credit destination.

        Runs inside a single PostgreSQL transaction with row-level locking.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
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
                    raise ValueError("quote_not_found")

                # ── Step 1.5: Idempotency check ─────────────────────────
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
                    raise ValueError("quote_already_executed")

                now = datetime.now(timezone.utc)
                if quote["expires_at"] < now:
                    await conn.execute(
                        "UPDATE quotes SET status = 'EXPIRED' WHERE id = $1",
                        quote_id,
                    )
                    raise ValueError("quote_expired")

                customer_id = quote["customer_id"]
                from_ccy = quote["from_currency"]
                to_ccy = quote["to_currency"]
                source_amount = quote["source_amount"]
                dest_amount = quote["dest_amount"]
                rate = quote["rate"]

                # ── Step 2: Lock balance rows (alphabetical order) ───────
                currencies = sorted([from_ccy, to_ccy])
                balances = {}
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
                        raise ValueError(f"no balance record for {ccy}")
                    balances[ccy] = balance_row["balance"]

                # ── Step 3: Check sufficient source balance ──────────────
                # Optimization: use pre-locked balance from dictionary
                source_balance = balances[from_ccy]
                if source_balance < source_amount:
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

                # ── Step 8: Store idempotency key ────────────────────────
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
