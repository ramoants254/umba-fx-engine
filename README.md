# Umba FX Engine

A production-grade, asynchronous Foreign Exchange engine supporting USD, EUR, KES, and NGN.
Built with **FastAPI**, **PostgreSQL** (raw `asyncpg` — no ORM), and **Pydantic**.

---

## Quick Start

Everything runs inside Docker. You only need `docker`, `docker compose`, and `make`.

```bash
make up        # Start API + Postgres
make test      # Run the full test suite
make load-test # Run Locust load test (headless, 50 users, 60s)
make down      # Tear down
```

No local Python environment is required. Dependencies are installed at image build time.

---

## Architecture Overview

```
Client
  │
  ▼
FastAPI (async, uvicorn)
  ├─ POST /quotes                → FXEngine.generate_quote()
  ├─ POST /quotes/{id}/execute   → FXEngine.execute_quote()
  ├─ GET  /customers             → customer CRUD
  ├─ GET  /rates                 → RateProvider.snapshot()
  ├─ GET  /healthz               → DB + rate freshness check
  └─ GET  /metrics               → in-process counters
        │
        ▼
   PostgreSQL (asyncpg, raw SQL)
   ├─ customers
   ├─ balances     (NUMERIC(20,4), CHECK balance >= 0)
   ├─ quotes       (PENDING → EXECUTED | EXPIRED)
   ├─ transactions (immutable ledger)
   └─ idempotency_keys
```

Rate data flows: upstream API (exchangeratesapi.io) → in-memory cache with 50 bps spread applied → engine.

---

## Key Engineering Decisions

### Why raw asyncpg over an ORM?
Complete control over lock ordering, isolation levels, and `SELECT ... FOR UPDATE` semantics.
ORMs obscure transaction boundaries and can silently reorder queries, creating deadlock risk.

### Why PostgreSQL over SQLite?
Row-level locking (`SELECT ... FOR UPDATE`) is required for concurrency safety.
SQLite's file-level write lock would serialize all executions and is insufficient for N-concurrent-quote testing.

### Concurrency & Deadlock Prevention
All executions follow a strict lock ordering:
1. Lock the **quote row** first.
2. Lock **balance rows** in alphabetical currency order (`sorted([from_ccy, to_ccy])`).

This deterministic ordering prevents deadlocks when two simultaneous executions involve overlapping currencies.

### Idempotency Key Implementation
The idempotency key is checked **inside the transaction, after the quote row is locked**.
This prevents the TOCTOU race: concurrent retries block on the lock, then find the committed cached response.
Checking outside the lock would allow two threads to simultaneously miss the cache and double-execute.

### Decimal Precision
All monetary values use `decimal.Decimal`. Rounding (`ROUND_HALF_UP`) is applied exactly once — at the final converted amount. Intermediate cross-rate calculations retain full precision.

---

## Running Tests

```bash
make test
```

The test suite covers:

| Module | What's Tested |
|--------|---------------|
| `test_atomicity.py` | Insufficient-balance rollback, quote status unchanged after failure, DB CHECK constraint defense |
| `test_concurrency.py` | 20 parallel executions of the same quote (exactly 1 succeeds), 5 parallel executions of different quotes (all succeed) |
| `test_idempotency.py` | Sequential retries, no double-debit, 10 concurrent retries return same `transaction_id` |
| `test_quotes.py` | Quote lifecycle, expiry, all 12 currency pairs, unknown customer |
| `test_decimals.py` | Hypothesis property-based: precision invariants, round-trip spread direction |
| `test_rates.py` | API failure fallback, stale threshold enforcement, spread math |

All 31 tests pass. Example run:

```
tests/test_atomicity.py ....     [ 12%]
tests/test_concurrency.py ..     [ 19%]
tests/test_decimals.py .....     [ 35%]
tests/test_idempotency.py ....   [ 48%]
tests/test_quotes.py .........   [ 77%]
tests/test_rates.py .......      [100%]
31 passed in 18.16s
```

---

## Rate-Source Failure Handling

When the upstream rates API is unavailable:

1. The cached rates remain in memory and continue to be served.
2. Each failed refresh attempt logs a structured `WARNING` with `stale_age_seconds`.
3. If rates are older than **15 minutes**, `is_usable()` returns `False`.
4. Any quote generation attempt while stale returns `503 Service Unavailable`.

This is tested in `test_rates.py::test_refresh_failure_keeps_stale_rates` and `test_rates_become_unusable_after_stale_threshold`.

---

## Example Log Output

All logs are JSON via `structlog`. A complete `quote → execute` trace:

```json
{"correlation_id": "9cf14dae-64d8-49ce-ae97-9e66cb4d1421", "event": "http_request", "method": "POST", "path": "/customers", "status": 201, "duration_ms": 14.2, "timestamp": "2026-07-18T14:45:00.123456Z", "level": "info"}
{"correlation_id": "31bfa82a-adbe-4835-965a-063a51601ca2", "event": "balance_credited", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "currency": "USD", "amount": "1000.00", "new_balance": "1000.00", "timestamp": "2026-07-18T14:45:05.654321Z", "level": "info"}
{"correlation_id": "7dcfa2d1-e6e7-4c48-8df0-827c81a2eb3a", "event": "quote_generated", "quote_id": "ef8a1c90-93a0-4a8b-b8df-39d73cb695f3", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "pair": "USD/KES", "source_amount": "100.00", "rate": "128.85250000", "dest_amount": "12885.25", "timestamp": "2026-07-18T14:45:10.789012Z", "level": "info"}
{"correlation_id": "d4cb8a2d-1a8e-4a6c-941f-82dc91a2ebbb", "event": "quote_executed", "transaction_id": "0b15b13b-8cf7-4f67-a068-15cf6cb4d789", "quote_id": "ef8a1c90-93a0-4a8b-b8df-39d73cb695f3", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "pair": "USD/KES", "source_amount": "100.00", "dest_amount": "12885.25", "timestamp": "2026-07-18T14:45:15.987654Z", "level": "info"}
```

The `correlation_id` links every log line across the full request lifecycle.

---

## API Reference

### Customers
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/customers` | Create a customer |
| `GET` | `/customers/{id}/balances` | View balances |
| `POST` | `/customers/{id}/balances/credit` | Credit a balance (test fixture) |

### Quotes & Execution
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/quotes` | Generate an FX quote (60s TTL) |
| `POST` | `/quotes/{id}/execute` | Execute a quote (atomic debit + credit) |

Send `Idempotency-Key: <uuid>` header on execute for safe client-side retries.

### Observability
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | DB ping + rate freshness check |
| `GET` | `/metrics` | In-process counters (quotes, errors, rate fetch stats) |
| `GET` | `/rates` | Current rate snapshot |

---

## AI Tooling

This project was built pair-programming with **Antigravity (Google DeepMind)** as the primary AI coding assistant — the same class of tool as GitHub Copilot, Cursor, and Claude Code. The role is explicitly AI-native, so the goal was to demonstrate effective supervision of AI-generated code rather than avoiding it.

Three concrete cases where the AI's output was caught and corrected:

1. **Idempotency TOCTOU:** The AI put the idempotency key check before the `SELECT ... FOR UPDATE`. Concurrent retries would both miss the cache and both execute. Fixed by moving the check inside the lock.

2. **Spread direction inverted:** The AI used the `sell` rate for direct conversion (giving the customer *more* currency than intended) and `1/buy` for inverse (also in the customer's favor). The Hypothesis test suite caught this — customers were making profit on round-trips. Fixed to use `buy` for direct and `1/sell` for inverse.

3. **ASGI lifespan in tests:** The AI assumed `ASGITransport` triggers FastAPI startup events. It does not. Tests failed with `FX engine not initialised`. Fixed by manually initialising and injecting dependencies in `conftest.py`.

---

## Known Limitations

- **In-memory rate cache:** In a multi-worker / multi-instance deployment, each worker has its own cache. A Redis-backed shared cache would synchronize rate freshness across nodes.
- **Single-region locking:** `SELECT ... FOR UPDATE` is sufficient for single-Postgres deployments. A distributed database setup would need distributed coordination (e.g., advisory locks or compare-and-swap).
- **No DDL migrations:** Schema is created at startup via inline DDL. Alembic would be the production choice for safe schema evolution.

---

## What We'd Do With Another Day

1. **Redis rate cache** — share rate snapshots across worker processes without redundant API calls.
2. **Alembic migrations** — replace startup DDL with versioned schema migrations.
3. **Retry with exponential backoff** on rate API failures (`tenacity`).
4. **Historical ledger queries** — daily volume per customer, per-currency limits.
5. **Prometheus metrics endpoint** — replace the custom JSON `/metrics` with a standard `/metrics` scrape target.

---

## Time Estimates

- **Wall-clock time:** ~1 day
- **Active engagement time:** ~5–6 hours

---
