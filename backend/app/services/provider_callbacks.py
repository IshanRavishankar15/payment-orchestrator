import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider import Provider
from app.models.provider_event import ProviderEvent
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.schemas.payment import PaymentState
from app.services.state_machine import (
    InvalidStateTransition,
    TransitionEvidence,
    transition_payment_in_transaction,
)


class CallbackReferenceNotFound(Exception):
    pass


def process_provider_callback(
    session_factory: sessionmaker[Session],
    provider_name: str,
    payload: dict[str, Any],
) -> str:
    event_type = payload["event_type"]
    target_state = (
        PaymentState.SUCCEEDED
        if event_type == "PAYMENT_SUCCEEDED"
        else PaymentState.FAILED
    )
    expected_outcome = (
        "SUCCEEDED" if target_state is PaymentState.SUCCEEDED else "DECLINED"
    )

    with session_factory() as session, session.begin():
        provider_id = session.scalar(
            select(Provider.id).where(Provider.name == provider_name)
        )
        if provider_id is None:
            raise CallbackReferenceNotFound()

        attempt = session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.provider_id == provider_id,
                PaymentAttempt.provider_reference == payload["provider_reference"],
            )
        )
        if attempt is None:
            raise CallbackReferenceNotFound()

        payment = session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.id == attempt.payment_id)
            .with_for_update()
        )
        if payment is None:
            raise CallbackReferenceNotFound()

        event_id = session.execute(
            insert(ProviderEvent)
            .values(
                provider_id=provider_id,
                provider_event_id=payload["provider_event_id"],
                event_type=event_type,
                payload=payload,
                signature_valid=True,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ProviderEvent.provider_id,
                    ProviderEvent.provider_event_id,
                ]
            )
            .returning(ProviderEvent.id)
        ).scalar_one_or_none()
        if event_id is None:
            return "DUPLICATE"

        event = session.get(ProviderEvent, event_id)
        if event is None:
            raise RuntimeError("Newly inserted provider event could not be loaded")
        transaction = session.scalar(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.provider_id == provider_id,
                ProviderSimulationTransaction.provider_reference
                == payload["provider_reference"],
            )
        )
        if (
            transaction is None
            or transaction.outcome != expected_outcome
            or transaction.accepted is not True
        ):
            event.processing_result = "CONTRADICTORY"
        elif payment.current_state in (
            PaymentState.SUCCEEDED.value,
            PaymentState.FAILED.value,
        ):
            event.processing_result = (
                "LATE_EVENT_IGNORED"
                if payment.current_state != target_state.value
                else "ALREADY_TERMINAL"
            )
        elif payment.current_state not in (
            PaymentState.PROCESSING.value,
            PaymentState.UNKNOWN.value,
        ):
            event.processing_result = "INVALID_PAYMENT_STATE"
        else:
            attempt.status = (
                "SUCCEEDED" if target_state is PaymentState.SUCCEEDED else "DECLINED"
            )
            attempt.finished_at = func.now()
            transition_payment_in_transaction(
                session,
                payment,
                target_state,
                "Authenticated provider callback confirmed outcome",
                TransitionEvidence.CALLBACK,
                attempt_id=attempt.id,
                provider_event_row_id=event.id,
            )
            event.processing_result = f"APPLIED_{target_state.value}"

        event.processed_at = func.now()
        return str(event.processing_result)
