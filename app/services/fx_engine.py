"""FX engine coordinator.

Main coordinator matching the public API contract, delegating quote generation
and execution to sub-services to stay under the 200-line limit.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from app.services.rate_provider import RateProvider
from app.services.quote_generator import QuoteGenerator, CURRENCY_QUANTUM, SUPPORTED_CURRENCIES
from app.services.quote_executor import QuoteExecutor
from app.services.rate_router import RateRouter


class FXEngine:
    """Core FX coordinator. Keeps track of stats and delegates logic."""

    def __init__(self, rate_provider: RateProvider) -> None:
        """Initialize coordinators and sub-services."""
        self.rates = rate_provider
        self.router = RateRouter(rate_provider)
        self.generator = QuoteGenerator(rate_provider)
        self.executor = QuoteExecutor()
        self._quotes_generated: int = 0
        self._quotes_executed: int = 0
        self._execution_errors: int = 0

    @property
    def quotes_generated(self) -> int:
        """Total quotes generated since startup."""
        return self._quotes_generated

    @property
    def quotes_executed(self) -> int:
        """Total quotes executed since startup."""
        return self._quotes_executed

    @property
    def execution_errors(self) -> int:
        """Total execution errors since startup."""
        return self._execution_errors

    async def generate_quote(
        self,
        customer_id: uuid.UUID,
        from_ccy: str,
        to_ccy: str,
        amount: Decimal,
    ) -> Dict[str, Any]:
        """Generate an FX quote and persist it."""
        try:
            res = await self.generator.generate(customer_id, from_ccy, to_ccy, amount)
            self._quotes_generated += 1
            return res
        except Exception:
            raise

    async def execute_quote(
        self,
        quote_id: uuid.UUID,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a pending quote atomically."""
        try:
            res = await self.executor.execute(quote_id, idempotency_key)
            self._quotes_executed += 1
            return res
        except Exception:
            self._execution_errors += 1
            raise

    def _effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Proxy method to resolve effective rate for compatibility with test suite."""
        return self.router.effective_rate(from_ccy, to_ccy)

