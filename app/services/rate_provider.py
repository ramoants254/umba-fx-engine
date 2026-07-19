"""Rate provider: fetches, caches, and applies spreads to FX rates.

Staleness policy (per SPEC.md §7):
  - Cache TTL: 5 minutes
  - Background refresh: every 60 seconds
  - Stale tolerance: up to 15 minutes after last successful fetch
  - Beyond 15 min stale: reject new quotes with 503
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

# All supported currencies
SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}

# Direct pairs for which we source mid-rates
DIRECT_PAIRS = [
    "USD/EUR", "USD/KES", "USD/NGN",
    "EUR/USD", "EUR/KES", "EUR/NGN",
]

# Seed mid-rates used when API is unavailable or for testing
_SEED_MID: Dict[str, Decimal] = {
    "USD/EUR": Decimal("0.92"),
    "USD/KES": Decimal("129.50"),
    "USD/NGN": Decimal("1480.00"),
    "EUR/USD": Decimal("1.087"),
    "EUR/KES": Decimal("140.75"),
    "EUR/NGN": Decimal("1608.50"),
}


def _apply_spread(mid: Decimal, spread_bps: Decimal) -> Dict[str, Decimal]:
    """Apply symmetric spread to a mid-rate, returning buy and sell."""
    return {
        "buy": mid * (Decimal("1") - spread_bps),
        "sell": mid * (Decimal("1") + spread_bps),
        "mid": mid,
    }


class RateProvider:
    """Fetches, caches, and serves FX rates with buy/sell spreads.

    Thread-safety note: rates dict is replaced atomically (dict assignment
    is atomic in CPython). The background refresh task runs in the same
    asyncio event loop, so no lock is needed.
    """

    def __init__(self) -> None:
        self._spread_bps = Decimal(settings.rate_spread_bps)
        self._rates: Dict[str, Dict[str, Decimal]] = {}
        self._last_updated: Optional[datetime] = None
        self._last_fetch_time: Optional[float] = None  # monotonic
        self._fetch_successes: int = 0
        self._fetch_failures: int = 0
        self._refresh_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ── Public API ───────────────────────────────────────────────────────

    def get(self, pair: str) -> Optional[Dict[str, Decimal]]:
        """Return buy/sell/mid for a pair, or None if not available."""
        return self._rates.get(pair)

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        """Return all rates as string values (for JSON serialisation)."""
        return {
            pair: {k: str(v) for k, v in data.items()}
            for pair, data in self._rates.items()
        }

    @property
    def last_updated(self) -> Optional[datetime]:
        return self._last_updated

    @property
    def fetch_successes(self) -> int:
        return self._fetch_successes

    @property
    def fetch_failures(self) -> int:
        return self._fetch_failures

    def rate_age_seconds(self) -> Optional[float]:
        """Seconds since the last successful rate fetch (monotonic)."""
        if self._last_fetch_time is None:
            return None
        return time.monotonic() - self._last_fetch_time

    def is_stale(self) -> bool:
        """True if rates are older than the stale-max threshold."""
        age = self.rate_age_seconds()
        if age is None:
            return True
        return age > settings.rate_stale_max_seconds

    def is_usable(self) -> bool:
        """True if rates are fresh enough to generate quotes."""
        return len(self._rates) > 0 and not self.is_stale()

    # ── Fetching ─────────────────────────────────────────────────────────

    async def refresh(self) -> bool:
        """Fetch fresh rates from the upstream API.

        Returns True if successful, False on failure. On failure, stale
        rates continue to be served (up to the staleness threshold).
        """
        try:
            mid_rates = await self._fetch_from_api()
            new_rates = {
                pair: _apply_spread(mid, self._spread_bps)
                for pair, mid in mid_rates.items()
            }
            # Atomic replacement
            self._rates = new_rates
            self._last_updated = datetime.now(timezone.utc)
            self._last_fetch_time = time.monotonic()
            self._fetch_successes += 1
            log.info(
                "rates_refreshed",
                pairs=len(new_rates),
                source="api",
            )
            return True
        except Exception:
            self._fetch_failures += 1
            age = self.rate_age_seconds()
            log.warning(
                "rate_fetch_failed",
                stale_age_seconds=age,
                will_serve_stale=not self.is_stale(),
                exc_info=True,
            )
            return False

    async def _fetch_from_api(self) -> Dict[str, Decimal]:
        """Hit the exchange-rates API and return mid-rates.

        Uses USD as the base currency to get EUR, KES, NGN rates,
        then derives the EUR-based pairs.
        """
        if not settings.rate_api_key:
            log.info("rate_api_key_not_set, using_seed_rates")
            return dict(_SEED_MID)

        mid_rates: Dict[str, Decimal] = {}

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch USD-based rates
            resp = await client.get(
                f"{settings.rate_api_base_url}/latest",
                params={
                    "access_key": settings.rate_api_key,
                    "symbols": "USD,EUR,KES,NGN",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success", False):
                raise ValueError(f"API error: {data.get('error', {})}")

            # exchangeratesapi.io returns rates relative to EUR (free tier)
            # so base=EUR, and we get USD, KES, NGN rates
            base_rates = data.get("rates", {})
            eur_to_usd = Decimal(str(base_rates.get("USD", "1")))
            eur_to_kes = Decimal(str(base_rates.get("KES", "140.75")))
            eur_to_ngn = Decimal(str(base_rates.get("NGN", "1608.50")))

            # EUR-based pairs
            mid_rates["EUR/USD"] = eur_to_usd
            mid_rates["EUR/KES"] = eur_to_kes
            mid_rates["EUR/NGN"] = eur_to_ngn

            # USD-based pairs (derived)
            usd_to_eur = Decimal("1") / eur_to_usd
            mid_rates["USD/EUR"] = usd_to_eur
            mid_rates["USD/KES"] = eur_to_kes / eur_to_usd
            mid_rates["USD/NGN"] = eur_to_ngn / eur_to_usd

        return mid_rates

    def load_seed_rates(self) -> None:
        """Load hardcoded seed rates (for testing / initial boot)."""
        self._rates = {
            pair: _apply_spread(mid, self._spread_bps)
            for pair, mid in _SEED_MID.items()
        }
        self._last_updated = datetime.now(timezone.utc)
        self._last_fetch_time = time.monotonic()
        log.info("seed_rates_loaded", pairs=len(self._rates))

    # ── Background refresh ───────────────────────────────────────────────

    async def start_background_refresh(self) -> None:
        """Start periodic rate refresh in the background."""
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        log.info(
            "background_refresh_started",
            interval=settings.rate_refresh_interval_seconds,
        )

    async def stop_background_refresh(self) -> None:
        """Cancel the background refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
            log.info("background_refresh_stopped")

    async def _refresh_loop(self) -> None:
        """Periodically refresh rates."""
        while True:
            await asyncio.sleep(settings.rate_refresh_interval_seconds)
            await self.refresh()
