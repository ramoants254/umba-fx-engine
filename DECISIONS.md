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

During collaboration, the AI made three critical design/logical errors that were caught and corrected:

1. **Idempotency Key Check Placement (TOCTOU):** 
   - *AI Suggestion:* The AI initially checked the idempotency key outside the quote locking step, claiming it optimized DB traffic.
   - *How We Caught It:* Under high concurrency, multiple concurrent retries with the same key would all check the cache simultaneously, see a cache-miss, bypass validation, and proceed to execute. We corrected this by shifting the idempotency check to happen **inside the database lock** (immediately after `SELECT ... FOR UPDATE` on the quote). This serializes duplicate calls so that subsequent threads block first, then cleanly read the committed cached result.
2. **Reverse/Inverse Spread Directions:**
   - *AI Suggestion:* The AI implemented rates utilizing the `sell` rate for direct conversion (e.g. USD → KES) and `1 / buy` for inverse conversion.
   - *How We Caught It:* Our Hypothesis test suite failed because the customer was making a profit on a round-trip USD → KES → USD. We corrected this to charge the spread against the customer in both directions: using `buy` for direct lookup (lower amount of KES) and `1 / sell` for inverse lookup (lower amount of USD).
3. **ASGI Lifespan in Tests:**
   - *AI Suggestion:* The AI assumed that the FastAPI startup lifespan would automatically execute during test client requests using `ASGITransport`.
   - *How We Caught It:* Tests failed with `RuntimeError: FX engine not initialised` because `ASGITransport` does not run the application startup/lifespan events. We bypassed the ASGI lifespan hook for tests and manually instantiated and injected the database connection pool, rate provider, and engine dependencies in `conftest.py`.

---

## 4. What Was Not Trusted Without Verification

- **Precision Rounding Rules:** We did not trust that intermediate calculations would maintain full decimal precision. We wrote explicit tests verifying that the engine does not perform intermediate rounding (e.g. rounding the cross-rate before multiplying by the source amount), and only quantizes at the very final step.
- **Concurrency & Deadlock Prevention:** We manually verified the alphabetical locking order of currency balance rows. If thread A locks KES then NGN, and thread B locks NGN then KES, a deadlock occurs. Enforcing strict alphabetical sort order (`sorted([from_ccy, to_ccy])`) ensures all execution paths lock rows in the exact same sequence.
- **Hypothesis Boundary Conditions:** We manually adjusted the property-based tests to ignore fractional sub-cent values (e.g., intermediate amounts < 1.00 unit) because rounding noise on microscopic amounts (like 0.01 KES) naturally dominates the spread.

