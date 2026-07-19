# Umba FX Engine Take-Home

Production-grade, asynchronous Foreign Exchange (FX) Engine built using **FastAPI**, **PostgreSQL (via asyncpg)**, and **Pydantic**.

This project is fully Dockerized. A helper `Makefile` is provided to allow running the app, executing the test suite, and running load tests entirely inside Docker without having to configure local Python virtual environments or databases.

---

## 1. Setup & Running the Application

### Prerequisites
- Docker & Docker Compose
- `make`

### Start the Application:
To build the images and launch the backend API and PostgreSQL database containers:
```bash
make up
```
This spins up:
- The PostgreSQL database container on port `5432`
- The FastAPI application container on port `8000`

Database tables and schemas are initialized automatically during application startup.

### Stop the Application:
To tear down the containers:
```bash
make down
```

---

## 2. Running Tests

The test suite contains unit tests, currency routing math checks, property-based tests using **Hypothesis**, concurrency stress tests, and atomicity verification.

To run all tests inside a Docker container:
```bash
make test
```
This boots a temporary test runner container, connects to the database, executes the tests, and reports outcomes.

---

## 3. Load Testing

We include a load test script using **Locust** to test throughput and latency under stress.

To run the load test inside Docker:
```bash
make load-test
```

---

## 4. Other Makefile Targets

- `make build`      - Build the Docker images
- `make restart`    - Restart the application container
- `make logs`       - Tail logs from the running containers
- `make status`     - Show current state of containers
- `make clean`      - Remove containers, volumes, and temporary files

---

## 5. Example Log Output

All logs are written as JSON structured entries via `structlog`. Below is an example execution trace:

```json
{"correlation_id": "9cf14dae-64d8-49ce-ae97-9e66cb4d1421", "event": "http_request", "method": "POST", "path": "/customers", "status": 201, "duration_ms": 14.2, "timestamp": "2026-07-18T14:45:00.123456Z", "level": "info"}
{"correlation_id": "31bfa82a-adbe-4835-965a-063a51601ca2", "event": "balance_credited", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "currency": "USD", "amount": "1000.00", "new_balance": "1000.00", "timestamp": "2026-07-18T14:45:05.654321Z", "level": "info"}
{"correlation_id": "7dcfa2d1-e6e7-4c48-8df0-827c81a2eb3a", "event": "quote_generated", "quote_id": "ef8a1c90-93a0-4a8b-b8df-39d73cb695f3", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "pair": "USD/KES", "source_amount": "100.00", "rate": "130.14750000", "dest_amount": "13014.75", "timestamp": "2026-07-18T14:45:10.789012Z", "level": "info"}
{"correlation_id": "d4cb8a2d-1a8e-4a6c-941f-82dc91a2ebbb", "event": "quote_executed", "transaction_id": "0b15b13b-8cf7-4f67-a068-15cf6cb4d789", "quote_id": "ef8a1c90-93a0-4a8b-b8df-39d73cb695f3", "customer_id": "d748f328-98e6-4bf4-b97c-9b81f1bcf5ff", "pair": "USD/KES", "source_amount": "100.00", "dest_amount": "13014.75", "timestamp": "2026-07-18T14:45:15.987654Z", "level": "info"}
```

---

## 6. Known Limitations

- **Single Database Node:** The system relies on PostgreSQL row locks (`SELECT ... FOR UPDATE`). In a multi-master distributed database setup, deterministic row locking requires distributed locking coordination (e.g. Redlock or database consensus strategies).
- **In-Memory Rate Cache:** Exchange rates are stored in-memory inside the `RateProvider` instance. In an auto-scaling environment with multiple worker nodes, cache nodes (like Redis) should be used to share rate snapshots across servers.

---

## 7. What We'd Do With Another Day

If we had another day to work on this, we would implement:
1. **Distributed Caching (Redis):** Share exchange rates across worker processes to avoid fetching from the upstream API on every independent node.
2. **Circuit Breaker Pattern:** Add resilience to the external exchange rates API fetches (e.g., using `tenacity` or a custom backoff policy) so that API rate-limit errors or transient network failures degrade gracefully.
3. **Database Migrations (Alembic):** Implement robust Alembic schemas instead of running inline DDL statements on startup.
4. **Historical Audits:** A separate ledger tracking daily volume per user and currency exchange limits.
