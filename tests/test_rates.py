"""Tests for rate-source failure handling.

Requirement: "What happens when the rates API is down, slow, or returns
stale data? Document the policy in SPEC.md and demonstrate it."
"""
from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services.rate_provider import RateProvider


@pytest.mark.asyncio
async def test_refresh_failure_keeps_stale_rates():
    """When upstream fetch fails, stale rates continue to be served."""
    provider = RateProvider()
    provider.load_seed_rates()

    # Rates are loaded and usable
    assert provider.is_usable()
    snapshot_before = provider.snapshot()

    # Simulate API failure
    with patch.object(
        provider, "_fetch_from_api", new_callable=AsyncMock, side_effect=Exception("timeout")
    ):
        success = await provider.refresh()

    assert success is False
    assert provider.fetch_failures == 1

    # Rates are still available (stale but within tolerance)
    assert provider.is_usable()
    assert provider.snapshot() == snapshot_before


@pytest.mark.asyncio
async def test_rates_become_unusable_after_stale_threshold():
    """After exceeding stale threshold, rates become unusable."""
    provider = RateProvider()
    provider.load_seed_rates()

    # Artificially age the rates beyond the stale threshold
    provider._last_fetch_time = time.monotonic() - 1000  # 1000 seconds ago

    assert provider.is_stale()
    assert not provider.is_usable()


@pytest.mark.asyncio
async def test_no_rates_loaded_is_unusable():
    """Provider with no rates loaded is not usable."""
    provider = RateProvider()
    assert not provider.is_usable()
    assert provider.rate_age_seconds() is None


@pytest.mark.asyncio
async def test_successful_refresh_resets_staleness():
    """After a successful refresh, rates are fresh again.

    We mock the external API call because test containers have no internet
    access. The important assertion is that a *successful* fetch (whatever
    the source) resets the staleness clock.
    """
    provider = RateProvider()
    provider.load_seed_rates()

    # Age the rates
    provider._last_fetch_time = time.monotonic() - 1000
    assert provider.is_stale()

    # Simulate a successful API response with realistic mid-rates
    mock_mid_rates = {
        "USD/EUR": Decimal("0.9200"),
        "USD/KES": Decimal("129.50"),
        "USD/NGN": Decimal("1550.00"),
        "EUR/USD": Decimal("1.0870"),
        "EUR/KES": Decimal("140.75"),
        "EUR/NGN": Decimal("1684.00"),
    }
    with patch.object(
        provider, "_fetch_from_api", new_callable=AsyncMock, return_value=mock_mid_rates
    ):
        success = await provider.refresh()

    assert success is True
    assert not provider.is_stale()
    assert provider.is_usable()


@pytest.mark.asyncio
async def test_fetch_failure_increments_counter():
    """Each fetch failure increments the failure counter."""
    provider = RateProvider()
    provider.load_seed_rates()

    with patch.object(
        provider, "_fetch_from_api", new_callable=AsyncMock, side_effect=Exception("err")
    ):
        await provider.refresh()
        await provider.refresh()
        await provider.refresh()

    assert provider.fetch_failures == 3
    assert provider.fetch_successes == 0


@pytest.mark.asyncio
async def test_seed_rates_cover_all_direct_pairs():
    """Seed rates must cover all required direct pairs."""
    provider = RateProvider()
    provider.load_seed_rates()

    required = [
        "USD/EUR", "USD/KES", "USD/NGN",
        "EUR/USD", "EUR/KES", "EUR/NGN",
    ]
    for pair in required:
        data = provider.get(pair)
        assert data is not None, f"Missing seed rate for {pair}"
        assert data["buy"] > 0
        assert data["sell"] > 0
        assert data["sell"] > data["buy"]  # spread exists


def test_spread_calculation():
    """Verify buy/sell spread is correctly applied."""
    provider = RateProvider()
    provider.load_seed_rates()

    usd_kes = provider.get("USD/KES")
    assert usd_kes is not None
    mid = usd_kes["mid"]

    # With 50bps spread:
    expected_buy = mid * (Decimal("1") - Decimal("0.005"))
    expected_sell = mid * (Decimal("1") + Decimal("0.005"))

    assert usd_kes["buy"] == expected_buy
    assert usd_kes["sell"] == expected_sell
