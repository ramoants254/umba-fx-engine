"""Quote and execution routes."""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

import structlog

from app.schemas import QuoteRequest, QuoteResponse, ExecuteResponse
from app.services.fx_engine import FXEngine

log = structlog.get_logger()
router = APIRouter(prefix="/quotes", tags=["quotes"])

# The engine instance is injected by the main app at startup
_engine: FXEngine | None = None


def set_engine(engine: FXEngine) -> None:
    """Set the FX engine instance (called at app startup)."""
    global _engine
    _engine = engine


def _get_engine() -> FXEngine:
    if _engine is None:
        raise RuntimeError("FX engine not initialised")
    return _engine


@router.post("", status_code=201, response_model=QuoteResponse)
async def create_quote(body: QuoteRequest, request: Request):
    """Generate an FX quote for a currency conversion."""
    cid = request.state.correlation_id
    engine = _get_engine()

    if body.from_currency == body.to_currency:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "detail": "from and to currencies must differ",
                "correlation_id": cid,
            },
        )

    try:
        result = await engine.generate_quote(
            customer_id=body.customer_id,
            from_ccy=body.from_currency,
            to_ccy=body.to_currency,
            amount=body.amount,
        )
    except ValueError as e:
        error_str = str(e)
        status = 404 if error_str == "customer_not_found" else 400
        return JSONResponse(
            status_code=status,
            content={
                "error": error_str,
                "correlation_id": cid,
            },
        )
    except RuntimeError as e:
        if "rates_unavailable" in str(e):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "rates_unavailable",
                    "detail": "Exchange rates are stale or unavailable",
                    "correlation_id": cid,
                },
            )
        raise

    return QuoteResponse(
        quote_id=result["quote_id"],
        customer_id=result["customer_id"],
        from_currency=result["from_currency"],
        to_currency=result["to_currency"],
        source_amount=result["source_amount"],
        rate=result["rate"],
        dest_amount=result["dest_amount"],
        created_at=result["created_at"],
        expires_at=result["expires_at"],
    )


@router.post("/{quote_id}/execute", response_model=ExecuteResponse)
async def execute_quote(
    quote_id: uuid.UUID,
    request: Request,
    idempotency_key: str | None = Header(
        None, alias="Idempotency-Key"
    ),
):
    """Execute a pending quote — atomic debit/credit."""
    cid = request.state.correlation_id
    engine = _get_engine()

    try:
        result = await engine.execute_quote(
            quote_id=quote_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        error_str = str(e)
        status_map = {
            "quote_not_found": 404,
            "quote_expired": 400,
            "quote_already_executed": 409,
            "insufficient_balance": 400,
        }
        status = status_map.get(error_str, 400)
        return JSONResponse(
            status_code=status,
            content={
                "error": error_str,
                "correlation_id": cid,
            },
        )

    return result
