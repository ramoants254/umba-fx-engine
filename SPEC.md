# FX Engine — Technical Specification

## 1. Scope

A foreign-exchange conversion engine supporting USD, EUR, KES, and NGN with
per-customer multi-currency balance accounts. The system exposes a JSON REST
API for generating quotes, executing conversions, and managing customers.

**Out of scope:** authentication, authorization, multi-tenancy, settlement,
regulatory reporting, audit trail beyond structured logs, UI.

---

## 2. Currency Pairs & Routing

### 2.1 Supported Currencies
USD, EUR, KES, NGN — four currencies yielding 12 ordered pairs.

### 2.2 Direct Pairs (sourced from rate provider)
USD/EUR, USD/KES, USD/NGN, EUR/KES, EUR/NGN, EUR/USD.

### 2.3 Rate Resolution Order
For a conversion `FROM → TO`:

1. **Direct lookup** — `FROM/TO` exists in provider → use `sell` rate.
2. **Inverse** — `TO/FROM` exists → use `1 / buy` rate (worst for customer
   on the inverse side, preserves spread revenue).
3. **Cross via USD** — resolve `FROM → USD` (step 1 or 2), then `USD → TO`
   (step 1 or 2). Rates multiply; spreads compound.
4. **Cross via EUR** — same logic with EUR as intermediary.
5. **Fail** — return `400` with `"no rate available"`.

### 2.4 Spread Model
Each direct pair has a `mid` rate from the upstream source.
- `buy  = mid × (1 − spread_bps)`  — rate at which the bank buys base ccy
- `sell = mid × (1 + spread_bps)`  — rate at which the bank sells base ccy

Default spread: **50 bps** each side (`spread_bps = 0.005`).

For cross pairs, each leg applies its own spread independently.
Effective cross rate = `sell(FROM/BRIDGE) × sell(BRIDGE/TO)`.

---

## 3. Decimal Precision & Rounding

### 3.1 Rules
- All monetary arithmetic uses Python `decimal.Decimal`. **No `float`.**
- Rounding mode: `ROUND_HALF_UP`.
- Rounding is applied **once**, at the final converted amount.
- Intermediate calculations (rate routing, cross-rate multiplication) keep
  full `Decimal` precision.

### 3.2 Per-Currency Minor Units

| Currency | Minor unit | Quantize step |
|----------|-----------|---------------|
| USD      | cent (2)  | `0.01`        |
| EUR      | cent (2)  | `0.01`        |
| KES      | cent (2)  | `0.01`        |
| NGN      | kobo (2)  | `0.01`        |

### 3.3 Rate Precision
Rates are stored and transmitted with up to **8 significant decimal digits**.
No rounding is applied to rates.

### 3.4 Balance Precision
Balances are stored as `NUMERIC(20, 4)` in PostgreSQL — 4 decimal places
internally to avoid cumulative rounding error. Displayed to 2 decimal places.

---

## 4. Quotes

### 4.1 Fields
`quote_id` (UUID), `customer_id`, `from_currency`, `to_currency`,
`source_amount`, `rate`, `dest_amount`, `created_at`, `expires_at`,
`status` (PENDING | EXECUTED | EXPIRED).

### 4.2 TTL
60 seconds from `created_at`. After expiry, execution attempts return
`400 quote expired`. Expired quotes are never garbage-collected during
the exercise; in production they'd be swept periodically.

### 4.3 Invariant
A quote locks in the rate and destination amount at generation time.
Execution uses the **quote's stored rate**, not a re-fetched live rate.

---

## 5. Execution

### 5.1 Atomicity
Execution is a single PostgreSQL transaction:

```
BEGIN
  SELECT ... FROM quotes WHERE id = $1 FOR UPDATE          -- lock quote
  -- validate: status = PENDING, not expired
  SELECT ... FROM balances WHERE customer_id = $2
           AND currency = $3 FOR UPDATE                    -- lock source balance
  SELECT ... FROM balances WHERE customer_id = $2
           AND currency = $4 FOR UPDATE                    -- lock dest balance
  -- validate: source_balance >= source_amount
  UPDATE balances SET balance = balance - source_amount ... -- debit
  UPDATE balances SET balance = balance + dest_amount ...   -- credit
  UPDATE quotes SET status = 'EXECUTED', executed_at = ... -- mark executed
  INSERT INTO transactions (...)                           -- audit record
  INSERT INTO idempotency_keys (...)                       -- cache response
COMMIT
```

If any validation fails, the transaction is rolled back. Both legs succeed
or neither does.

### 5.2 Concurrency
Row-level locking via `SELECT ... FOR UPDATE`. Multiple concurrent
executions of the same quote: exactly one acquires the lock first; the
rest find `status != PENDING` after acquiring the lock and are rejected.

Lock ordering: quote row first, then balance rows in alphabetical currency
order (deterministic → no deadlocks).

### 5.3 Idempotency
Clients may send an `Idempotency-Key` header. The key is stored **inside**
the execution transaction. On retry:
- If the key exists in `idempotency_keys`, return the stored response (200).
- If the key does not exist, proceed with normal execution.

Checking the idempotency key outside the transaction creates a race
condition under concurrent retries — this implementation avoids that.

### 5.4 Insufficient Balance
If `source_balance < source_amount`, the transaction rolls back and returns
`400 {"error": "insufficient_balance"}`. No partial execution.

---

## 6. Customer Balances

### 6.1 Model
`(customer_id UUID, currency TEXT, balance NUMERIC(20,4))` — composite
primary key on `(customer_id, currency)`.

### 6.2 Invariant
`balance >= 0` always. Enforced by check constraint AND application-level
validation inside the execution transaction.

### 6.3 Operations
- **Create customer** — initializes zero balances for all four currencies.
- **View balances** — returns all currency balances for a customer.
- **Credit balance** — admin endpoint for test fixtures. Adds to balance.
  Rejects negative credit amounts.

---

## 7. Rate Provider

### 7.1 Source
Primary: exchangeratesapi.io free tier (or equivalent public API).
Fallback: seeded stub rates for development/testing.

### 7.2 Caching & Staleness Policy
- Rates are cached in memory after fetch.
- Cache TTL: **5 minutes**.
- Background refresh: every **60 seconds**.
- If upstream fetch fails:
  - Serve stale rates for up to **15 minutes** from last successful fetch.
  - Log WARNING on each stale-serve.
  - After 15 minutes stale, reject new quotes with `503 Service Unavailable`.
- `last_updated` timestamp exposed via `/rates` and `/healthz`.

### 7.3 Rate Limiting
Outbound requests to the rate API are rate-limited to avoid hitting free-tier
caps. Maximum **1 request per 60 seconds**.

---

## 8. Observability

### 8.1 Correlation IDs
Every request gets a `correlation_id` (from `X-Request-Id` header or
auto-generated UUID). All log entries for that request include it. Quote
creation and execution are linked via `quote_id` in logs.

### 8.2 Structured Logging
JSON-formatted logs via `structlog`. Fields: `timestamp`, `level`, `event`,
`correlation_id`, `customer_id` (when applicable), `quote_id` (when
applicable), `duration_ms`.

### 8.3 Health Check (`GET /healthz`)
Returns `{"status": "healthy"}` with 200, or `{"status": "unhealthy",
"reason": "..."}` with 503. Checks:
- Database connectivity
- Rate staleness (unhealthy if > 15 min stale)

### 8.4 Metrics (`GET /metrics`)
JSON object with counters and gauges:
- `quotes_generated`, `quotes_executed`, `quotes_expired`
- `execution_errors` (by type)
- `rate_fetch_successes`, `rate_fetch_failures`
- `rate_age_seconds`

---

## 9. Error Semantics

| Condition                  | HTTP | Body `error` field           |
|----------------------------|------|------------------------------|
| Malformed request          | 400  | `invalid_request`            |
| Quote not found            | 404  | `quote_not_found`            |
| Quote expired              | 400  | `quote_expired`              |
| Quote already executed     | 409  | `quote_already_executed`     |
| Insufficient balance       | 400  | `insufficient_balance`       |
| No rate available          | 400  | `no_rate_available`          |
| Rates stale / unavailable  | 503  | `rates_unavailable`          |
| Customer not found         | 404  | `customer_not_found`         |
| Internal error             | 500  | `internal_error`             |

All error responses include `correlation_id`.

---

## 10. Assumptions

- Single-region, single-instance deployment (no distributed locking needed).
- All timestamps are UTC.
- Customer IDs are UUIDs generated server-side.
- No currency-specific business rules beyond minor units.
- The free-tier rate API may not have all pairs; missing pairs are derived
  via the routing rules in §2.3.
