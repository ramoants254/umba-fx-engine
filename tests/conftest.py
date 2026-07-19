"""Shared test fixtures and configuration."""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Point at test database BEFORE importing app modules so Settings picks it up
os.environ["FX_DATABASE_URL"] = os.environ.get(
    "FX_TEST_DATABASE_URL",
    "postgresql://fx_user:fx_password@localhost:5432/fx_engine_test",
)

from app.database import get_pool, init_db, close_db  # noqa: E402
from app.services.rate_provider import RateProvider  # noqa: E402
from app.services.fx_engine import FXEngine  # noqa: E402
import app.routes.quotes as quotes_route  # noqa: E402
import app.routes.rates as rates_route  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def setup_db():
    """Initialise the database, rate provider, and engine once for the test session.

    We bypass the FastAPI lifespan here because ASGITransport does not
    trigger it. Instead we initialise the DB pool, inject seed rates, and
    wire the engine + rate provider directly into the route modules.
    """
    # Create the test database if it doesn't exist
    test_db_url = os.environ["FX_DATABASE_URL"]
    base_url, _db_name = test_db_url.rsplit("/", 1)
    sys_url = f"{base_url}/postgres"

    sys_conn = await asyncpg.connect(sys_url)
    try:
        exists = await sys_conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'fx_engine_test'"
        )
        if not exists:
            await sys_conn.execute("CREATE DATABASE fx_engine_test")
    finally:
        await sys_conn.close()

    await init_db()

    # Initialise rate provider with seed rates (no external API call)
    rate_provider = RateProvider()
    rate_provider.load_seed_rates()

    # Initialise FX engine
    engine = FXEngine(rate_provider)

    # Inject into route modules (mimics what lifespan does in production)
    quotes_route.set_engine(engine)
    rates_route.set_dependencies(rate_provider, engine)

    yield rate_provider, engine

    await close_db()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(setup_db):
    """Truncate all tables before each test for isolation."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE idempotency_keys, transactions, quotes, balances, customers
            CASCADE
            """
        )
    yield


@pytest_asyncio.fixture
async def client(setup_db) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client wired to the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def customer_id(client: AsyncClient) -> uuid.UUID:
    """Create a test customer and return their ID."""
    resp = await client.post("/customers", json={"name": "Test User"})
    assert resp.status_code == 201
    return uuid.UUID(resp.json()["id"])


@pytest_asyncio.fixture
async def funded_customer(client: AsyncClient, customer_id: uuid.UUID) -> uuid.UUID:
    """Create a test customer with funded balances."""
    for ccy in ["USD", "EUR", "KES", "NGN"]:
        resp = await client.post(
            f"/customers/{customer_id}/balances/credit",
            json={"currency": ccy, "amount": "100000"},
        )
        assert resp.status_code == 200
    return customer_id
