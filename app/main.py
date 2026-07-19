"""FastAPI application entry point.

Wires together: database, rate provider, FX engine, routes,
middleware (correlation IDs, error handling), and lifecycle events.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db, close_db
from app.logging_config import setup_logging
from app.services.rate_provider import RateProvider
from app.services.fx_engine import FXEngine
from app.routes.customers import router as customers_router
from app.routes.quotes import router as quotes_router, set_engine
from app.routes.rates import (
    rates_router,
    health_router,
    metrics_router,
    set_dependencies,
)

# ── Logging ──────────────────────────────────────────────────────────────────

setup_logging(debug=settings.debug)
log = structlog.get_logger()

# ── Shared instances ─────────────────────────────────────────────────────────

rate_provider = RateProvider()
engine = FXEngine(rate_provider)


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, load rates, start background refresh.
    Shutdown: stop refresh, close DB pool.
    """
    # Startup
    await init_db()

    # Load initial rates (seed if no API key)
    rate_provider.load_seed_rates()
    await rate_provider.refresh()  # attempt live fetch; seed remains if it fails
    await rate_provider.start_background_refresh()

    # Inject dependencies into route modules
    set_engine(engine)
    set_dependencies(rate_provider, engine)

    log.info("app_started", port=settings.port)
    yield

    # Shutdown
    await rate_provider.stop_background_refresh()
    await close_db()
    log.info("app_stopped")


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="FX Engine",
    description="Foreign exchange conversion engine with per-customer balances",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Middleware ───────────────────────────────────────────────────────────────

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Attach a correlation ID to every request.

    Uses X-Request-Id header if provided, otherwise generates a UUID.
    The ID is propagated via structlog contextvars and returned in
    the response header.
    """
    correlation_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id

    # Bind to structlog context for all log lines in this request
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 1)

    response.headers["X-Correlation-Id"] = correlation_id

    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions."""
    cid = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    log.exception("unhandled_exception", correlation_id=cid, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "correlation_id": cid,
        },
    )


# ── Routes ───────────────────────────────────────────────────────────────────

app.include_router(customers_router)
app.include_router(quotes_router)
app.include_router(rates_router)
app.include_router(health_router)
app.include_router(metrics_router)
