import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.models.merchant import Merchant
from app.models.payment_intent import PaymentIntent
from app.schemas.payment import (
    PaymentIntentCreate,
    PaymentIntentList,
    PaymentIntentRead,
    PaymentState,
)
from app.services.payment_intents import (
    IdempotencyKeyConflict,
    create_payment_intent,
)

router = APIRouter(prefix="/payments", tags=["payments"])


def _read_model(payment: PaymentIntent, merchant_id: str) -> PaymentIntentRead:
    return PaymentIntentRead(
        id=payment.id,
        merchant_id=merchant_id,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        state=payment.current_state,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


@router.post(
    "",
    response_model=PaymentIntentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    payload: PaymentIntentCreate,
    response: Response,
    session: Session = Depends(get_db),
) -> PaymentIntentRead:
    try:
        payment, merchant_id, created = create_payment_intent(session, payload)
    except IdempotencyKeyConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_payload_mismatch",
                "message": "This merchant has already used the idempotency key with a different payload.",
            },
        ) from error

    if not created:
        response.status_code = status.HTTP_200_OK
    return _read_model(payment, merchant_id)


@router.get("", response_model=PaymentIntentList)
def list_payments(
    merchant_id: str | None = Query(default=None, min_length=1, max_length=100),
    state: PaymentState | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> PaymentIntentList:
    statement = (
        select(PaymentIntent, Merchant.external_id)
        .join(Merchant, Merchant.id == PaymentIntent.merchant_id)
        .order_by(PaymentIntent.created_at.desc(), PaymentIntent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if merchant_id is not None:
        statement = statement.where(Merchant.external_id == merchant_id.strip())
    if state is not None:
        statement = statement.where(PaymentIntent.current_state == state.value)

    items = [
        _read_model(payment, external_id)
        for payment, external_id in session.execute(statement).all()
    ]
    return PaymentIntentList(items=items, limit=limit, offset=offset)


@router.get("/{payment_id}", response_model=PaymentIntentRead)
def get_payment(
    payment_id: uuid.UUID,
    session: Session = Depends(get_db),
) -> PaymentIntentRead:
    result = session.execute(
        select(PaymentIntent, Merchant.external_id)
        .join(Merchant, Merchant.id == PaymentIntent.merchant_id)
        .where(PaymentIntent.id == payment_id)
    ).one_or_none()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "payment_not_found",
                "message": "The requested payment intent does not exist.",
            },
        )
    return _read_model(result[0], result[1])
