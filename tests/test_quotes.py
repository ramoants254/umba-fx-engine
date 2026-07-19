"""Tests for quote generation and basic execution flow."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_quote_success(client: AsyncClient, funded_customer: uuid.UUID):
    """Generate a valid FX quote and verify response shape."""
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "quote_id" in data
    assert data["from_currency"] == "USD"
    assert data["to_currency"] == "KES"
    assert Decimal(data["source_amount"]) == Decimal("100")
    assert Decimal(data["dest_amount"]) > 0
    assert Decimal(data["rate"]) > 0
    assert data["expires_at"] > data["created_at"]


@pytest.mark.asyncio
async def test_create_quote_same_currency_rejected(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Reject quote where from == to currency."""
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "USD",
            "amount": "100",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_quote_zero_amount_rejected(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Reject quote with zero amount."""
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "0",
        },
    )
    assert resp.status_code == 422  # Pydantic validation (gt=0)


@pytest.mark.asyncio
async def test_create_quote_unknown_customer(client: AsyncClient):
    """Reject quote for non-existent customer."""
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(uuid.uuid4()),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "100",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execute_quote_success(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Execute a valid quote and verify balances change."""
    # Get initial balances
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    initial = {
        b["currency"]: Decimal(b["balance"])
        for b in bal_resp.json()["balances"]
    }

    # Create and execute quote
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100",
        },
    )
    quote_id = quote_resp.json()["quote_id"]
    source_amount = Decimal(quote_resp.json()["source_amount"])
    dest_amount = Decimal(quote_resp.json()["dest_amount"])

    exec_resp = await client.post(f"/quotes/{quote_id}/execute")
    assert exec_resp.status_code == 200
    assert "transaction_id" in exec_resp.json()

    # Verify balances
    bal_resp = await client.get(f"/customers/{funded_customer}/balances")
    final = {
        b["currency"]: Decimal(b["balance"])
        for b in bal_resp.json()["balances"]
    }

    assert final["USD"] == initial["USD"] - source_amount
    assert final["KES"] == initial["KES"] + dest_amount


@pytest.mark.asyncio
async def test_execute_nonexistent_quote(client: AsyncClient):
    """Reject execution of a non-existent quote."""
    resp = await client.post(f"/quotes/{uuid.uuid4()}/execute")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execute_already_executed_quote(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Reject double execution of the same quote (without idempotency key)."""
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(funded_customer),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "50",
        },
    )
    quote_id = quote_resp.json()["quote_id"]

    # First execution succeeds
    resp1 = await client.post(f"/quotes/{quote_id}/execute")
    assert resp1.status_code == 200

    # Second execution fails
    resp2 = await client.post(f"/quotes/{quote_id}/execute")
    assert resp2.status_code == 409
    assert resp2.json()["error"] == "quote_already_executed"


@pytest.mark.asyncio
async def test_execute_insufficient_balance(
    client: AsyncClient, customer_id: uuid.UUID
):
    """Reject execution when customer has insufficient balance (unfunded)."""
    # customer_id has zero balances (not funded_customer)
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": str(customer_id),
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "100",
        },
    )
    assert quote_resp.status_code == 201
    quote_id = quote_resp.json()["quote_id"]

    resp = await client.post(f"/quotes/{quote_id}/execute")
    assert resp.status_code == 400
    assert resp.json()["error"] == "insufficient_balance"


@pytest.mark.asyncio
async def test_all_currency_pairs(
    client: AsyncClient, funded_customer: uuid.UUID
):
    """Every valid currency pair can generate a quote."""
    currencies = ["USD", "EUR", "KES", "NGN"]
    for from_ccy in currencies:
        for to_ccy in currencies:
            if from_ccy == to_ccy:
                continue
            resp = await client.post(
                "/quotes",
                json={
                    "customer_id": str(funded_customer),
                    "from_currency": from_ccy,
                    "to_currency": to_ccy,
                    "amount": "10",
                },
            )
            assert resp.status_code == 201, (
                f"Failed for {from_ccy}/{to_ccy}: {resp.json()}"
            )
