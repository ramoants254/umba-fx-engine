"""Async PostgreSQL connection pool and schema initialization."""
from __future__ import annotations

import asyncpg
import structlog

from app.config import settings

log = structlog.get_logger()

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the global connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call init_db() first.")
    return _pool


async def init_db() -> None:
    """Create the connection pool and run schema DDL."""
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    log.info("database_initialised")


async def close_db() -> None:
    """Drain and close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("database_closed")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS balances (
    customer_id UUID NOT NULL REFERENCES customers(id),
    currency    TEXT NOT NULL,
    balance     NUMERIC(20, 4) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, currency),
    CONSTRAINT positive_balance CHECK (balance >= 0)
);

CREATE TABLE IF NOT EXISTS quotes (
    id              UUID PRIMARY KEY,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    from_currency   TEXT NOT NULL,
    to_currency     TEXT NOT NULL,
    source_amount   NUMERIC(20, 4) NOT NULL,
    rate            NUMERIC(20, 8) NOT NULL,
    dest_amount     NUMERIC(20, 4) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    executed_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS transactions (
    id              UUID PRIMARY KEY,
    quote_id        UUID NOT NULL REFERENCES quotes(id),
    customer_id     UUID NOT NULL REFERENCES customers(id),
    from_currency   TEXT NOT NULL,
    to_currency     TEXT NOT NULL,
    source_amount   NUMERIC(20, 4) NOT NULL,
    dest_amount     NUMERIC(20, 4) NOT NULL,
    rate            NUMERIC(20, 8) NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    quote_id    UUID NOT NULL,
    response    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
