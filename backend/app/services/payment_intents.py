import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.durable_job import DurableJob
from app.models.merchant import Merchant
from app.models.payment_intent import PaymentIntent
from app.schemas.payment import PaymentIntentCreate, PaymentState
from app.providers.base import ProviderScenario
from app.services.state_machine import record_initial_state_in_transaction


class IdempotencyKeyConflict(Exception):
    pass


def _request_fingerprint(payload: PaymentIntentCreate) -> str:
    canonical_payload = json.dumps(
        {
            "amount_minor": payload.amount_minor,
            "currency": payload.currency,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _find_payment(
    session: Session, payment_id: uuid.UUID
) -> tuple[PaymentIntent, str]:
    result = session.execute(
        select(PaymentIntent, Merchant.external_id)
        .join(Merchant, Merchant.id == PaymentIntent.merchant_id)
        .where(PaymentIntent.id == payment_id)
    ).one()
    return result[0], result[1]


def create_payment_intent(
    session: Session,
    payload: PaymentIntentCreate,
    scenario: ProviderScenario = ProviderScenario.SUCCESS,
    provider_name: str = "provider_a",
) -> tuple[PaymentIntent, str, bool]:
    fingerprint = _request_fingerprint(payload)
    created = False

    with session.begin():
        merchant_id = session.execute(
            insert(Merchant)
            .values(external_id=payload.merchant_id)
            .on_conflict_do_nothing(index_elements=[Merchant.external_id])
            .returning(Merchant.id)
        ).scalar_one_or_none()
        if merchant_id is None:
            merchant_id = session.execute(
                select(Merchant.id).where(Merchant.external_id == payload.merchant_id)
            ).scalar_one()

        proposed_payment_id = uuid.uuid4()
        payment_id = session.execute(
            insert(PaymentIntent)
            .values(
                id=proposed_payment_id,
                merchant_id=merchant_id,
                amount_minor=payload.amount_minor,
                currency=payload.currency,
                idempotency_key=payload.idempotency_key,
                request_fingerprint=fingerprint,
                current_state=PaymentState.CREATED.value,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PaymentIntent.merchant_id,
                    PaymentIntent.idempotency_key,
                ]
            )
            .returning(PaymentIntent.id)
        ).scalar_one_or_none()

        if payment_id is None:
            existing = session.execute(
                select(PaymentIntent).where(
                    PaymentIntent.merchant_id == merchant_id,
                    PaymentIntent.idempotency_key == payload.idempotency_key,
                )
            ).scalar_one()
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyKeyConflict()
            payment_id = existing.id
        else:
            created = True
            payment = session.get(PaymentIntent, payment_id)
            if payment is None:
                raise RuntimeError("Newly inserted payment could not be loaded")
            record_initial_state_in_transaction(
                session,
                payment,
            )
            session.add(
                DurableJob(
                    job_type="PROCESS_PAYMENT",
                    payment_id=payment_id,
                    dedupe_key=f"process-payment:{payment_id}",
                    scenario=scenario.value,
                    provider_name=provider_name,
                )
            )

        payment, merchant_external_id = _find_payment(session, payment_id)

    return payment, merchant_external_id, created
