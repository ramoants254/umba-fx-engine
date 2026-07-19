"""Concurrency safety test: N parallel executions of the same quote.

Requirement: "A test that fires N parallel executions of the same quote ID
and asserts exactly one succeeds."
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_concurrent_execute_exactly_one_succeeds(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Fire 20 parallel executions of the same quote — exactly 1 wins.

    The others must get either 409 (already executed) or 400 (quote expired).
    No double-execution, no double-debit.
    """
    # Create a quote
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "100",
        },
    )
    assert quote_resp.status_code == 201
    quote_id = quote_resp.json()["quote_id"]
    dest_amount = quote_resp.json()["dest_amount"]

    # Get initial balances
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    initial_usd = next(
        b["balance"]
        for b in bal_resp.json()["balances"]
        if b["currency"] == "USD"
    )

    # Fire N concurrent executions
    n = 20
    transport = ASGITransport(app=app)

    async def try_execute(i: int) -> dict:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(f"/quotes/{quote_id}/execute")
            return {"index": i, "status": resp.status_code, "body": resp.json()}

    results = await asyncio.gather(*[try_execute(i) for i in range(n)])

    # Exactly one should succeed with 200
    successes = [r for r in results if r["status"] == 200]
    failures = [r for r in results if r["status"] != 200]

    assert len(successes) == 1, (
        f"Expected exactly 1 success, got {len(successes)}: {successes}"
    )

    # All failures should be 409 (already executed)
    for f in failures:
        assert f["status"] in (400, 409), (
            f"Unexpected status {f['status']}: {f['body']}"
        )

    # Verify balance was debited exactly once
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    final_usd = next(
        b["balance"]
        for b in bal_resp.json()["balances"]
        if b["currency"] == "USD"
    )
    from decimal import Decimal
    assert Decimal(final_usd) == Decimal(initial_usd) - Decimal("100"), (
        f"Balance debited incorrectly: initial={initial_usd}, final={final_usd}"
    )


@pytest.mark.asyncio
async def test_concurrent_different_quotes_all_succeed(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Multiple different quotes can execute concurrently without conflict."""
    # Create 5 different quotes
    quote_ids = []
    for _ in range(5):
        resp = await client.post(
            "/quotes",
            json={
                "customer_id": str(funded_customer),
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "10",
            },
        )
        assert resp.status_code == 201
        quote_ids.append(resp.json()["quote_id"])

    # Execute all concurrently
    transport = ASGITransport(app=app)

    async def try_execute(qid: str) -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(f"/quotes/{qid}/execute")
            return resp.status_code

    results = await asyncio.gather(*[try_execute(qid) for qid in quote_ids])

    # All should succeed
    assert all(s == 200 for s in results), f"Not all succeeded: {results}"
