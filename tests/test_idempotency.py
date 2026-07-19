"""Idempotency tests: retries with the same key must not double-execute.

Requirement: "Client retries with the same idempotency key must not
double-execute. Test it."
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_idempotent_retry_returns_same_result(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Two sequential calls with the same idempotency key return identical responses."""
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "100",
        },
    )
    quote_id = quote_resp.json()["quote_id"]
    idem_key = f"test-key-{uuid.uuid4()}"

    # First execution
    resp1 = await client.post(
        f"/quotes/{quote_id}/execute",
        headers={"Idempotency-Key": idem_key},
    )
    assert resp1.status_code == 200

    # Second execution with same key — should return cached response
    resp2 = await client.post(
        f"/quotes/{quote_id}/execute",
        headers={"Idempotency-Key": idem_key},
    )
    assert resp2.status_code == 200
    assert resp1.json()["transaction_id"] == resp2.json()["transaction_id"]


@pytest.mark.asyncio
async def test_idempotent_retry_no_double_debit(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Idempotent retries must not debit the balance twice."""
    # Get initial balance
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    initial_usd = Decimal(
        next(
            b["balance"]
            for b in bal_resp.json()["balances"]
            if b["currency"] == "USD"
        )
    )

    # Create quote
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "500",
        },
    )
    quote_id = quote_resp.json()["quote_id"]
    idem_key = f"no-double-{uuid.uuid4()}"

    # Execute 3 times with same key
    for _ in range(3):
        resp = await client.post(
            f"/quotes/{quote_id}/execute",
            headers={"Idempotency-Key": idem_key},
        )
        assert resp.status_code == 200

    # Balance should be debited exactly once
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    final_usd = Decimal(
        next(
            b["balance"]
            for b in bal_resp.json()["balances"]
            if b["currency"] == "USD"
        )
    )
    assert final_usd == initial_usd - Decimal("500")


@pytest.mark.asyncio
async def test_concurrent_idempotent_retries(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """N concurrent retries with the same idempotency key → exactly one execution."""
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "200",
        },
    )
    quote_id = quote_resp.json()["quote_id"]
    idem_key = f"concurrent-idem-{uuid.uuid4()}"

    n = 10
    transport = ASGITransport(app=app)

    async def try_with_key(i: int) -> dict:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                f"/quotes/{quote_id}/execute",
                headers={"Idempotency-Key": idem_key},
            )
            return {"status": resp.status_code, "body": resp.json()}

    results = await asyncio.gather(*[try_with_key(i) for i in range(n)])

    # All should return 200 (either fresh execution or idempotent replay)
    statuses = [r["status"] for r in results]
    assert all(s == 200 for s in statuses), f"Unexpected statuses: {statuses}"

    # All should return the same transaction_id
    tx_ids = {r["body"]["transaction_id"] for r in results}
    assert len(tx_ids) == 1, f"Multiple transaction IDs: {tx_ids}"

    # Balance debited exactly once
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    final_usd = Decimal(
        next(
            b["balance"]
            for b in bal_resp.json()["balances"]
            if b["currency"] == "USD"
        )
    )
    expected = Decimal("100000") - Decimal("200")
    assert final_usd == expected


@pytest.mark.asyncio
async def test_different_idempotency_keys_are_independent(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Different idempotency keys on different quotes execute independently."""
    quote_ids = []
    for _ in range(3):
        resp = await client.post(
            "/quotes",
            json={
                "customer_id": str(funded_customer),
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "10",
            },
        )
        quote_ids.append(resp.json()["quote_id"])

    tx_ids = set()
    for i, qid in enumerate(quote_ids):
        resp = await client.post(
            f"/quotes/{qid}/execute",
            headers={"Idempotency-Key": f"key-{i}-{uuid.uuid4()}"},
        )
        assert resp.status_code == 200
        tx_ids.add(resp.json()["transaction_id"])

    # Each should have a unique transaction ID
    assert len(tx_ids) == 3
