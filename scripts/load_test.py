"""Locust load test for the FX engine.

Usage:
    locust -f scripts/load_test.py --headless -u 50 -r 5 -t 60s --host http://localhost:8000

This creates a customer, funds it, then hammers quote → execute in a loop.
"""
from __future__ import annotations

import uuid

from locust import HttpUser, between, task


class FXUser(HttpUser):
    """Simulates a customer generating and executing FX quotes."""

    wait_time = between(0.1, 0.5)

    def on_start(self):
        """Create and fund a customer on session start."""
        resp = self.client.post("/customers", json={"name": f"loadtest-{uuid.uuid4()}"})
        self.customer_id = resp.json()["id"]

        for ccy in ["USD", "EUR", "KES", "NGN"]:
            self.client.post(
                f"/customers/{self.customer_id}/balances/credit",
                json={"currency": ccy, "amount": "10000000"},
            )

    @task(5)
    def quote_and_execute(self):
        """Generate a quote and immediately execute it."""
        resp = self.client.post(
            "/quotes",
            json={
                "customer_id": self.customer_id,
                "from_currency": "USD",
                "to_currency": "KES",
                "amount": "100",
            },
            name="/quotes [create]",
        )
        if resp.status_code != 201:
            return

        quote_id = resp.json()["quote_id"]
        self.client.post(
            f"/quotes/{quote_id}/execute",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            name="/quotes/{id}/execute",
        )

    @task(1)
    def check_balances(self):
        """Check customer balances."""
        self.client.get(
            f"/customers/{self.customer_id}/balances",
            name="/customers/{id}/balances",
        )

    @task(1)
    def check_rates(self):
        """Fetch current rates."""
        self.client.get("/rates", name="/rates")

    @task(1)
    def health_check(self):
        """Hit the health endpoint."""
        self.client.get("/healthz", name="/healthz")
