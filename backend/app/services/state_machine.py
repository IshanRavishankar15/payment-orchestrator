import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider_event import ProviderEvent
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.models.state_transition import StateTransition
from app.schemas.payment import PaymentState


class TransitionEvidence(StrEnum):
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    PROVIDER_RESPONSE = "PROVIDER_RESPONSE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_STATUS = "PROVIDER_STATUS"
    CALLBACK = "CALLBACK"
    RECONCILIATION = "RECONCILIATION"


class PaymentNotFound(Exception):
    pass


class InvalidStateTransition(Exception):
    pass


def record_initial_state_in_transaction(
    session: Session,
    payment: PaymentIntent,
    reason: str = "Payment intent created",
) -> StateTransition:
    if payment.current_state != PaymentState.CREATED.value:
        raise InvalidStateTransition(
            "A new payment's initial state must be CREATED"
        )
    transition = StateTransition(
        payment_id=payment.id,
        from_state=None,
        to_state=PaymentState.CREATED.value,
        reason=reason,
    )
    session.add(transition)
    session.flush()
    return transition


_ALLOWED_TRANSITIONS = {
    PaymentState.CREATED: {PaymentState.PROCESSING},
    PaymentState.PROCESSING: {
        PaymentState.SUCCEEDED,
        PaymentState.FAILED,
        PaymentState.UNKNOWN,
    },
    PaymentState.UNKNOWN: {PaymentState.SUCCEEDED, PaymentState.FAILED},
    PaymentState.SUCCEEDED: set(),
    PaymentState.FAILED: set(),
}

_ALLOWED_EVIDENCE = {
    (PaymentState.CREATED, PaymentState.PROCESSING): {
        TransitionEvidence.ATTEMPT_STARTED,
    },
    (PaymentState.PROCESSING, PaymentState.SUCCEEDED): {
        TransitionEvidence.PROVIDER_RESPONSE,
        TransitionEvidence.CALLBACK,
    },
    (PaymentState.PROCESSING, PaymentState.FAILED): {
        TransitionEvidence.PROVIDER_RESPONSE,
        TransitionEvidence.CALLBACK,
    },
    (PaymentState.PROCESSING, PaymentState.UNKNOWN): {
        TransitionEvidence.PROVIDER_TIMEOUT,
    },
    (PaymentState.UNKNOWN, PaymentState.SUCCEEDED): {
        TransitionEvidence.PROVIDER_STATUS,
        TransitionEvidence.CALLBACK,
        TransitionEvidence.RECONCILIATION,
    },
    (PaymentState.UNKNOWN, PaymentState.FAILED): {
        TransitionEvidence.PROVIDER_STATUS,
        TransitionEvidence.CALLBACK,
        TransitionEvidence.RECONCILIATION,
    },
}


def transition_payment_in_transaction(
    session: Session,
    payment: PaymentIntent,
    to_state: PaymentState,
    reason: str,
    evidence: TransitionEvidence,
    attempt_id: uuid.UUID | None = None,
    provider_event_row_id: uuid.UUID | None = None,
) -> StateTransition:
    try:
        from_state = PaymentState(payment.current_state)
    except ValueError as error:
        raise InvalidStateTransition("Payment has an unrecognized current state") from error

    edge = (from_state, to_state)
    if to_state not in _ALLOWED_TRANSITIONS[from_state]:
        raise InvalidStateTransition(
            f"Transition from {from_state.value} to {to_state.value} is not allowed"
        )
    if evidence not in _ALLOWED_EVIDENCE.get(edge, set()):
        raise InvalidStateTransition(
            f"Evidence {evidence.value} cannot support this transition"
        )
    if not reason.strip() or len(reason) > 255:
        raise ValueError("Transition reason must contain 1 to 255 characters")

    if attempt_id is None:
        raise InvalidStateTransition("State transitions require a provider attempt")
    attempt = session.get(PaymentAttempt, attempt_id)
    if attempt is None or attempt.payment_id != payment.id:
        raise InvalidStateTransition("Transition attempt must belong to the payment")

    if evidence is TransitionEvidence.ATTEMPT_STARTED and attempt.status != "IN_PROGRESS":
        raise InvalidStateTransition("The attempt has not been started")
    if evidence is TransitionEvidence.PROVIDER_TIMEOUT and attempt.status != "UNKNOWN":
        raise InvalidStateTransition("The attempt does not record an uncertain outcome")
    if evidence is TransitionEvidence.PROVIDER_RESPONSE:
        expected_attempt_status = {
            PaymentState.SUCCEEDED: "SUCCEEDED",
            PaymentState.FAILED: "DECLINED",
        }.get(to_state)
        if attempt.status != expected_attempt_status:
            raise InvalidStateTransition(
                "The provider response does not match the attempt outcome"
            )
    if evidence in (
        TransitionEvidence.PROVIDER_STATUS,
        TransitionEvidence.RECONCILIATION,
    ):
        provider_transaction = session.scalar(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.provider_id == attempt.provider_id,
                ProviderSimulationTransaction.provider_reference
                == attempt.provider_reference,
            )
        )
        supported_outcomes = {
            PaymentState.SUCCEEDED: ("SUCCEEDED", True),
            PaymentState.FAILED: ("DECLINED", True),
        }
        accepted_outcome = supported_outcomes.get(to_state)
        definitive_nonacceptance = (
            to_state is PaymentState.FAILED
            and provider_transaction is not None
            and provider_transaction.outcome == "NOT_ACCEPTED"
            and provider_transaction.accepted is False
        )
        if provider_transaction is None or not (
            (
                accepted_outcome is not None
                and provider_transaction.outcome == accepted_outcome[0]
                and provider_transaction.accepted is accepted_outcome[1]
            )
            or definitive_nonacceptance
        ):
            raise InvalidStateTransition(
                "The provider status does not authoritatively support this outcome"
            )

    if evidence is TransitionEvidence.CALLBACK:
        if provider_event_row_id is None:
            raise InvalidStateTransition("Callback transitions require a provider event")
        if session.get(ProviderEvent, provider_event_row_id) is None:
            raise InvalidStateTransition("Provider event does not exist")
    elif provider_event_row_id is not None:
        raise InvalidStateTransition(
            "Provider event references are only valid for callback transitions"
        )

    payment.current_state = to_state.value
    transition = StateTransition(
        payment_id=payment.id,
        from_state=from_state.value,
        to_state=to_state.value,
        reason=reason,
        attempt_id=attempt_id,
        provider_event_row_id=provider_event_row_id,
    )
    session.add(transition)
    session.flush()
    return transition


def transition_payment(
    session: Session,
    payment_id: uuid.UUID,
    to_state: PaymentState,
    reason: str,
    evidence: TransitionEvidence,
    attempt_id: uuid.UUID | None = None,
    provider_event_row_id: uuid.UUID | None = None,
) -> StateTransition:
    with session.begin():
        payment = session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.id == payment_id)
            .with_for_update()
        )
        if payment is None:
            raise PaymentNotFound()
        transition = transition_payment_in_transaction(
            session,
            payment,
            to_state,
            reason,
            evidence,
            attempt_id,
            provider_event_row_id,
        )
    return transition
