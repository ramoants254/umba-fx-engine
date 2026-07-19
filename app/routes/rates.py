"""Rate and health/metrics routes."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import structlog

from app.schemas import (
    RatePair,
    RatesSnapshot,
    HealthResponse,
    MetricsResponse,
)
from app.services.rate_provider import RateProvider
from app.services.fx_engine import FXEngine
from app.database import get_pool

log = structlog.get_logger()

# Injected at startup
_rate_provider: RateProvider | None = None
_engine: FXEngine | None = None


def set_dependencies(rate_provider: RateProvider, engine: FXEngine) -> None:
    """Set service dependencies (called at app startup)."""
    global _rate_provider, _engine
    _rate_provider = rate_provider
    _engine = engine


# ── Rates ────────────────────────────────────────────────────────────────────

rates_router = APIRouter(prefix="/rates", tags=["rates"])


@rates_router.get("", response_model=RatesSnapshot)
async def get_rates():
    """Return a snapshot of all current exchange rates."""
    assert _rate_provider is not None
    snap = _rate_provider.snapshot()
    pairs = [
        RatePair(pair=pair, buy=data["buy"], sell=data["sell"])
        for pair, data in snap.items()
    ]
    return RatesSnapshot(
        rates=pairs,
        last_updated=_rate_provider.last_updated,
        is_stale=_rate_provider.is_stale(),
    )


@rates_router.post("/refresh")
async def refresh_rates(request: Request):
    """Force a rate refresh from the upstream source."""
    assert _rate_provider is not None
    cid = request.state.correlation_id
    success = await _rate_provider.refresh()
    if success:
        return {
            "status": "ok",
            "last_updated": _rate_provider.last_updated.isoformat()
            if _rate_provider.last_updated
            else None,
            "correlation_id": cid,
        }
    return JSONResponse(
        status_code=503,
        content={
            "error": "rate_refresh_failed",
            "detail": "Upstream rate source unavailable",
            "correlation_id": cid,
        },
    )


# ── Health ───────────────────────────────────────────────────────────────────

health_router = APIRouter(tags=["health"])


@health_router.get("/healthz", response_model=HealthResponse)
async def health_check():
    """System health check: database connectivity and rate freshness."""
    assert _rate_provider is not None

    # Check DB
    db_status = "ok"
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        db_status = "unavailable"

    # Check rates
    rate_age = _rate_provider.rate_age_seconds()
    if _rate_provider.is_stale():
        rates_status = "stale"
    elif rate_age is None:
        rates_status = "not_loaded"
    else:
        rates_status = "fresh"

    overall = "healthy" if db_status == "ok" and rates_status == "fresh" else "unhealthy"
    status_code = 200 if overall == "healthy" else 503

    resp = HealthResponse(
        status=overall,
        db=db_status,
        rates_status=rates_status,
        rates_age_seconds=round(rate_age, 1) if rate_age is not None else None,
    )

    if status_code != 200:
        return JSONResponse(status_code=status_code, content=resp.model_dump())
    return resp


# ── Metrics ──────────────────────────────────────────────────────────────────

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics", response_model=MetricsResponse)
async def get_metrics():
    """Observability metrics for the FX engine."""
    assert _rate_provider is not None
    assert _engine is not None

    return MetricsResponse(
        quotes_generated=_engine.quotes_generated,
        quotes_executed=_engine.quotes_executed,
        execution_errors=_engine.execution_errors,
        rate_fetch_successes=_rate_provider.fetch_successes,
        rate_fetch_failures=_rate_provider.fetch_failures,
        rate_age_seconds=(
            round(_rate_provider.rate_age_seconds(), 1)
            if _rate_provider.rate_age_seconds() is not None
            else None
        ),
    )
