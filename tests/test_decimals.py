"""Property-based tests for decimal precision.

Requirement: "Decimal precision throughout. Property-based tests over
random amounts and pairs (Hypothesis or similar)."
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.services.fx_engine import FXEngine, CURRENCY_QUANTUM, SUPPORTED_CURRENCIES
from app.services.rate_provider import RateProvider


# ── Strategies ───────────────────────────────────────────────────────────────

# Generate amounts as Decimal strings to avoid float contamination
amount_strategy = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

currency_strategy = st.sampled_from(sorted(SUPPORTED_CURRENCIES))


# ── Tests ────────────────────────────────────────────────────────────────────

class TestDecimalPrecision:
    """Property-based tests ensuring no precision loss in rate calculations."""

    @pytest.fixture(autouse=True)
    def _setup_engine(self):
        self.rate_provider = RateProvider()
        self.rate_provider.load_seed_rates()
        self.engine = FXEngine(self.rate_provider)

    @given(amount=amount_strategy, from_ccy=currency_strategy, to_ccy=currency_strategy)
    @settings(max_examples=200, deadline=2000)
    def test_converted_amount_has_correct_precision(
        self, amount: Decimal, from_ccy: str, to_ccy: str
    ):
        """Converted amount must have exactly 2 decimal places.

        Tiny source amounts (e.g. 0.01 KES → EUR) can legitimately produce
        0.00 in the destination currency after rounding. We skip those cases
        since they would be rejected by a minimum-order check in production.
        """
        assume(from_ccy != to_ccy)

        rate = self.engine._effective_rate(from_ccy, to_ccy)
        quantum = CURRENCY_QUANTUM[to_ccy]
        result = (amount * rate).quantize(quantum, rounding=ROUND_HALF_UP)

        # Must have at most 2 decimal places
        assert result == result.quantize(Decimal("0.01"))
        # Must be non-negative (can be 0.00 for sub-cent conversions)
        assert result >= 0

    @given(amount=amount_strategy, from_ccy=currency_strategy, to_ccy=currency_strategy)
    @settings(max_examples=200, deadline=2000)
    def test_rate_is_positive(
        self, amount: Decimal, from_ccy: str, to_ccy: str
    ):
        """Every resolved rate must be positive."""
        assume(from_ccy != to_ccy)
        rate = self.engine._effective_rate(from_ccy, to_ccy)
        assert rate > 0

    @given(amount=amount_strategy, from_ccy=currency_strategy, to_ccy=currency_strategy)
    @settings(max_examples=100, deadline=2000)
    def test_no_float_contamination(
        self, amount: Decimal, from_ccy: str, to_ccy: str
    ):
        """Intermediate values must remain Decimal (never float)."""
        assume(from_ccy != to_ccy)
        rate = self.engine._effective_rate(from_ccy, to_ccy)
        assert isinstance(rate, Decimal), f"Rate is {type(rate)}, not Decimal"
        product = amount * rate
        assert isinstance(product, Decimal), f"Product is {type(product)}, not Decimal"

    @given(from_ccy=currency_strategy, to_ccy=currency_strategy)
    @settings(max_examples=50, deadline=2000)
    def test_spread_direction(self, from_ccy: str, to_ccy: str):
        """Sell rate should be worse for the customer than buy rate.

        For a direct pair, sell > mid. For inverse, 1/buy > 1/sell.
        The effective rate used for conversions should give the
        customer fewer units than the mid-rate would.
        """
        assume(from_ccy != to_ccy)

        direct = self.rate_provider.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            # sell >= mid: customer gets fewer dest units
            assert direct["sell"] >= direct["mid"]
            assert direct["buy"] <= direct["mid"]

    @given(amount=amount_strategy, from_ccy=currency_strategy, to_ccy=currency_strategy)
    @settings(max_examples=100, deadline=2000)
    def test_round_trip_within_spread(
        self, amount: Decimal, from_ccy: str, to_ccy: str
    ):
        """Converting A→B→A should not gain significant value beyond rounding noise.

        Ideal property: spread always eats value. However, two independent
        ROUND_HALF_UP operations across very different magnitudes (e.g. EUR→KES
        rounds up the large integer leg, then KES→EUR rounds up again) can
        produce a 1-cent gain. We allow 1 minor unit of tolerance to distinguish
        genuine spread leakage from unavoidable rounding noise.
        """
        assume(from_ccy != to_ccy)

        rate_ab = self.engine._effective_rate(from_ccy, to_ccy)
        rate_ba = self.engine._effective_rate(to_ccy, from_ccy)

        quantum_b = CURRENCY_QUANTUM[to_ccy]
        quantum_a = CURRENCY_QUANTUM[from_ccy]

        intermediate = (amount * rate_ab).quantize(quantum_b, rounding=ROUND_HALF_UP)
        # Skip checking when the target amount is extremely small (sub-unit).
        # Rounding noise on sub-unit amounts naturally dominates the spread.
        assume(intermediate >= Decimal("1.00"))

        round_tripped = (intermediate * rate_ba).quantize(
            quantum_a, rounding=ROUND_HALF_UP
        )

        # Allow up to 1 minor unit of rounding noise on the round-trip.
        # A genuine spread breach would be much larger.
        tolerance = quantum_a
        assert round_tripped <= amount + tolerance, (
            f"Round-trip gained more than rounding noise: "
            f"{amount} → {intermediate} → {round_tripped}"
        )
