"""Rate routing and conversion calculation service."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.services.rate_provider import RateProvider


class RateRouter:
    """Resolves effective exchange rates across direct, inverse, and cross pairs.

    Encapsulates spread rules and bridge path selection (SPEC.md §2.3).
    """

    def __init__(self, rate_provider: RateProvider) -> None:
        """Initialize the router with a rate provider."""
        self.rates = rate_provider

    def effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Resolve the effective rate for a currency conversion.

        Resolution order (SPEC.md §2.3):
          1. Direct pair → sell rate
          2. Inverse pair → 1 / buy rate
          3. Cross via USD (two legs, spreads compound)
          4. Cross via EUR (two legs, spreads compound)
          5. Fail

        Args:
            from_ccy: Source currency code.
            to_ccy: Destination currency code.

        Returns:
            The effective Decimal rate.

        Raises:
            ValueError: If no rate path exists.
        """
        # 1. Direct lookup
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        # 2. Inverse lookup
        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            # 1/sell gives the worst rate for the customer (preserves spread)
            return Decimal("1") / inverse["sell"]

        # 3. Cross via USD
        rate = self._try_cross(from_ccy, to_ccy, "USD")
        if rate is not None:
            return rate

        # 4. Cross via EUR
        rate = self._try_cross(from_ccy, to_ccy, "EUR")
        if rate is not None:
            return rate

        raise ValueError(f"no rate available for {from_ccy}/{to_ccy}")

    def _try_cross(
        self, from_ccy: str, to_ccy: str, bridge: str
    ) -> Optional[Decimal]:
        """Attempt a two-leg cross rate through a bridge currency.

        Each leg resolves independently (direct buy or inverse 1/sell).
        Rates multiply, so spreads compound naturally.

        Returns None if either leg cannot be resolved.
        """
        if from_ccy == bridge or to_ccy == bridge:
            return None

        leg1 = self._resolve_single_leg(from_ccy, bridge)
        if leg1 is None:
            return None

        leg2 = self._resolve_single_leg(bridge, to_ccy)
        if leg2 is None:
            return None

        return leg1 * leg2

    def _resolve_single_leg(
        self, from_ccy: str, to_ccy: str
    ) -> Optional[Decimal]:
        """Resolve a single leg: direct buy or inverse 1/sell."""
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            return Decimal("1") / inverse["sell"]

        return None
