# Design Decisions & Trade-Offs

## 1. Key Trade-offs

### Architecture & Framework
- **FastAPI (async) with Uvicorn:** We chose FastAPI instead of Flask to provide native async I/O support. In a high-throughput financial system, network-bound calls (database queries, rate fetching) should not block execution threads.
- **PostgreSQL over SQLite:** While SQLite is simpler, we chose PostgreSQL because the concurrency safety requirement (multiple parallel executions of the same quote) demands robust row-level locking (`SELECT ... FOR UPDATE`). SQLite's locking is coarse-grained (file-level lock during writes), which limits throughput and handles concurrent transactions less elegantly in a real-world scenario.

### Database Layer
- **Raw SQL (asyncpg) over ORM:** We avoided ORMs like SQLAlchemy ORM or Tortoise to maintain absolute control over the SQL query structure, isolation levels, and locking semantics. ORMs add hidden overhead and can obscure transactional boundaries or deadlock risks. Raw `asyncpg` enables precise database-level transactional flow.

### Decimal Precision
- **Decimal Everywhere:** Every amount, rate, and balance is represented using Python's `decimal.Decimal`. Rounding is exclusively applied at the very final output step (`ROUND_HALF_UP` quantized to 2 decimal places). This prevents cumulative rounding errors that occur when intermediate calculations are prematurely rounded.

---

## 2. AI Delegation & Collaboration

### Human-Owned Decisions
- **Locking strategy:** The choice of using explicit row-level locks on the quote and balance rows in a strict alphabetical currency order to prevent deadlocks was defined entirely by the developer.
- **Transaction boundaries:** Ensuring that idempotency key lookup and database status check/updates happen inside a single database transaction rather than checking in application memory beforehand.

### AI-Delegated Tasks
- **Pydantic schema definitions:** Generated boilerplate for request and response models.
- **Test templates:** Generated test cases for normal operation, property-based testing structures, and standard route mappings.

---

## 3. What the AI Got Wrong (and How We Caught It)

During initial architectural suggestions, the AI proposed checking the idempotency key outside the database transaction, arguing it would save DB connections on duplicate calls.

**How we caught it:** We realized this is a classic time-of-check to time-of-use (TOCTOU) bug. Under high concurrency, two rapid duplicate execute requests could check the cache simultaneously, both find a miss, and both execute. We rejected this optimization and forced the idempotency check inside the SQL transaction block.

---

## 4. What Was Not Trusted Without Verification

- **Rate routing calculations:** We manually verified the math behind compound rates for cross-currency pairs. Spreads compound multiplicatively rather than additively, and we enforced that inverse pairs correctly use `1 / buy` to preserve the bank's spread revenue (instead of using `1 / mid` which would yield no profit margin).
- **Concurrency tests:** We wrote a custom stress test (`test_concurrency.py`) firing 20 simultaneous execution tasks for a single quote ID to prove that the Postgres `SELECT ... FOR UPDATE` lock successfully prevents double-execution.
