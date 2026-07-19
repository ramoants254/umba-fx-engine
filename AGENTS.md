# AGENTS.md — AI Coding Constraints

## Architecture

- **Framework:** FastAPI (async) with uvicorn.
- **Database:** PostgreSQL via asyncpg. No ORM — use explicit SQL queries.
- **All monetary values** use Python `decimal.Decimal`. Never use `float`
  for amounts, rates, or balances. This is non-negotiable.

## Coding Standards

- Python 3.11+. Type hints on all function signatures.
- Use `async def` for all route handlers and service methods that touch DB or HTTP.
- Structured logging via `structlog` — every log line is JSON with
  `correlation_id`, `event`, `timestamp`.
- No wildcard imports. No unused imports.
- Docstrings on all public functions (Google-style).
- Keep files under 200 lines. Split when they grow.

## Transaction Rules

- Execution (debit + credit) happens in a **single PostgreSQL transaction**.
- Use `SELECT ... FOR UPDATE` for row-level locking.
- Lock order: quote row first, then balance rows in alphabetical currency
  order to prevent deadlocks.
- Idempotency key check and storage happen **inside** the same transaction.
- Never check a condition outside a lock and then act on it inside — TOCTOU.

## Testing Requirements

- `pytest` with `pytest-asyncio` for async tests.
- `hypothesis` for property-based tests on decimal precision.
- Concurrency test: N parallel async tasks executing the same quote.
- Every test must clean up after itself (use transaction rollback or truncate).

## What NOT to Do

- Do not add authentication or authorization.
- Do not use an ORM (SQLAlchemy ORM, Tortoise, etc.) — raw asyncpg.
- Do not use `threading.Lock` for concurrency control — use DB locks.
- Do not round intermediate calculations — round only the final output.
- Do not re-fetch live rates at execution time — use the rate stored in the quote.
- Do not put business logic in route handlers — delegate to service layer.
