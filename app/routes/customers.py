"""Customer management routes."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import structlog

from app.schemas import (
    CustomerCreate,
    CustomerResponse,
    CustomerBalancesResponse,
    BalanceResponse,
    CreditRequest,
    CreditResponse,
)
from app.services.customer_service import CustomerService

log = structlog.get_logger()
router = APIRouter(prefix="/customers", tags=["customers"])

# Service instantiation
customer_service = CustomerService()


@router.post("", status_code=201, response_model=CustomerResponse)
async def create_customer(body: CustomerCreate, request: Request):
    """Create a new customer with zero balances in all currencies."""
    cid = request.state.correlation_id
    result = await customer_service.create_customer(body.name)

    # Bind correlation ID to logs
    structlog.contextvars.bind_contextvars(correlation_id=cid)

    return CustomerResponse(
        id=result["id"],
        name=result["name"],
        created_at=result["created_at"],
    )


@router.get("/{customer_id}/balances", response_model=CustomerBalancesResponse)
async def get_balances(customer_id: uuid.UUID, request: Request):
    """Return all currency balances for a customer."""
    try:
        balances = await customer_service.get_balances(customer_id)
    except ValueError as e:
        if str(e) == "customer_not_found":
            return JSONResponse(
                status_code=404,
                content={
                    "error": "customer_not_found",
                    "correlation_id": request.state.correlation_id,
                },
            )
        raise

    balances_resp = [
        BalanceResponse(currency=b["currency"], balance=b["balance"])
        for b in balances
    ]
    return CustomerBalancesResponse(
        customer_id=customer_id, balances=balances_resp
    )


@router.post("/{customer_id}/balances/credit", response_model=CreditResponse)
async def credit_balance(
    customer_id: uuid.UUID, body: CreditRequest, request: Request
):
    """Credit (add to) a customer's balance in a specific currency."""
    cid = request.state.correlation_id
    try:
        result = await customer_service.credit_balance(
            customer_id=customer_id,
            currency=body.currency,
            amount=body.amount,
        )
    except ValueError as e:
        if str(e) == "customer_not_found":
            return JSONResponse(
                status_code=404,
                content={
                    "error": "customer_not_found",
                    "correlation_id": cid,
                },
            )
        raise

    return CreditResponse(
        customer_id=result["customer_id"],
        currency=result["currency"],
        new_balance=result["new_balance"],
    )
