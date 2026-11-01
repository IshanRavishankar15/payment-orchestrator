import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.models.durable_job import DurableJob
from app.models.merchant import Merchant
from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider import Provider
from app.models.provider_event import ProviderEvent
from app.models.state_transition import StateTransition
from app.schemas.payment import (
    PaymentAttemptRead,
    PaymentIntentCreate,
    PaymentIntentList,
    PaymentIntentRead,
    PaymentState,
    PaymentTimelineRead,
    ProviderEventRead,
    ReconciliationJobRead,
    StateTransitionRead,
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


@router.get("/{payment_id}/timeline", response_model=PaymentTimelineRead)
def get_payment_timeline(
    payment_id: uuid.UUID,
    session: Session = Depends(get_db),
) -> PaymentTimelineRead:
    payment_result = session.execute(
        select(PaymentIntent, Merchant.external_id)
        .join(Merchant, Merchant.id == PaymentIntent.merchant_id)
        .where(PaymentIntent.id == payment_id)
    ).one_or_none()
    if payment_result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "payment_not_found",
                "message": "The requested payment intent does not exist.",
            },
        )

    attempts = session.execute(
        select(PaymentAttempt, Provider.name)
        .join(Provider, Provider.id == PaymentAttempt.provider_id)
        .where(PaymentAttempt.payment_id == payment_id)
        .order_by(PaymentAttempt.attempt_number)
    ).all()
    provider_references = [
        attempt.provider_reference
        for attempt, _ in attempts
        if attempt.provider_reference is not None
    ]

    events = []
    if provider_references:
        events = session.execute(
            select(ProviderEvent, Provider.name)
            .join(Provider, Provider.id == ProviderEvent.provider_id)
            .where(
                ProviderEvent.payload["provider_reference"]
                .as_string()
                .in_(provider_references)
            )
            .order_by(ProviderEvent.received_at, ProviderEvent.id)
        ).all()

    transitions = session.scalars(
        select(StateTransition)
        .where(StateTransition.payment_id == payment_id)
        .order_by(StateTransition.occurred_at, StateTransition.id)
    ).all()
    reconciliation_jobs = session.scalars(
        select(DurableJob)
        .where(
            DurableJob.payment_id == payment_id,
            DurableJob.job_type == "RECONCILE_PAYMENT",
        )
        .order_by(DurableJob.created_at, DurableJob.id)
    ).all()

    return PaymentTimelineRead(
        payment=_read_model(payment_result[0], payment_result[1]),
        attempts=[
            PaymentAttemptRead(
                id=attempt.id,
                provider_name=provider_name,
                attempt_number=attempt.attempt_number,
                provider_reference=attempt.provider_reference,
                status=attempt.status,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                error_code=attempt.error_code,
                error_message=attempt.error_message,
                reason=attempt.reason,
            )
            for attempt, provider_name in attempts
        ],
        transitions=[
            StateTransitionRead(
                id=transition.id,
                from_state=transition.from_state,
                to_state=transition.to_state,
                reason=transition.reason,
                attempt_id=transition.attempt_id,
                provider_event_row_id=transition.provider_event_row_id,
                occurred_at=transition.occurred_at,
            )
            for transition in transitions
        ],
        provider_events=[
            ProviderEventRead(
                id=event.id,
                provider_name=provider_name,
                provider_event_id=event.provider_event_id,
                event_type=event.event_type,
                signature_valid=event.signature_valid,
                received_at=event.received_at,
                processed_at=event.processed_at,
                processing_result=event.processing_result,
            )
            for event, provider_name in events
        ],
        reconciliation_jobs=[
            ReconciliationJobRead(
                id=job.id,
                attempt_id=job.attempt_id,
                status=job.status,
                retry_count=job.retry_count,
                max_retries=job.max_retries,
                available_at=job.available_at,
                lease_expires_at=job.lease_expires_at,
                last_error=job.last_error,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in reconciliation_jobs
        ],
    )
