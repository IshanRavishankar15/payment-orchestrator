import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider import Provider
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.models.state_transition import StateTransition
from app.providers.base import ProviderOutcome, ProviderScenario
from app.schemas.payment import PaymentState
from app.services.payment_orchestration import (
    get_provider_status,
    get_provider_transaction,
    process_payment,
)
from app.services.state_machine import (
    InvalidStateTransition,
    TransitionEvidence,
    transition_payment,
)


def _create_payment() -> uuid.UUID:
    payload = {
        "merchant_id": f"phase3-merchant-{uuid.uuid4()}",
        "amount_minor": 4312,
        "currency": "INR",
        "idempotency_key": f"phase3-request-{uuid.uuid4()}",
    }
    with TestClient(app) as client:
        response = client.post("/payments", json=payload)
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def _payment_state(payment_id: uuid.UUID) -> str:
    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        assert payment is not None
        return payment.current_state


@pytest.mark.parametrize(
    ("scenario", "expected_state", "expected_attempt_status"),
    [
        (ProviderScenario.SUCCESS, "SUCCEEDED", "SUCCEEDED"),
        (ProviderScenario.DECLINE, "FAILED", "DECLINED"),
    ],
)
def test_provider_response_transitions_payment_and_records_history(
    scenario: ProviderScenario,
    expected_state: str,
    expected_attempt_status: str,
) -> None:
    payment_id = _create_payment()

    attempt = process_payment(SessionLocal, payment_id, scenario)

    assert attempt.status == expected_attempt_status
    assert attempt.attempt_number == 1
    assert attempt.reason == "Initial attempt routed to provider_a"
    assert _payment_state(payment_id) == expected_state

    with SessionLocal() as session:
        provider_name = session.scalar(
            select(Provider.name).where(Provider.id == attempt.provider_id)
        )
        transitions = session.scalars(
            select(StateTransition)
            .where(StateTransition.payment_id == payment_id)
            .order_by(StateTransition.occurred_at, StateTransition.id)
        ).all()
    assert provider_name == "provider_a"
    assert [transition.to_state for transition in transitions] == [
        "CREATED",
        "PROCESSING",
        expected_state,
    ]
    assert transitions[1].attempt_id == attempt.id
    assert transitions[2].attempt_id == attempt.id


def test_lost_provider_response_is_unknown_and_status_query_resolves_evidence() -> None:
    payment_id = _create_payment()

    attempt = process_payment(
        SessionLocal,
        payment_id,
        ProviderScenario.SUCCESS_LOST_RESPONSE,
    )

    assert attempt.status == "UNKNOWN"
    assert attempt.error_code == "provider_timeout"
    assert _payment_state(payment_id) == "UNKNOWN"
    result = get_provider_status(SessionLocal, attempt.id)
    assert result is not None
    assert result.outcome is ProviderOutcome.SUCCEEDED
    assert result.accepted is True
    with SessionLocal() as session:
        transition_payment(
            session,
            payment_id,
            PaymentState.SUCCEEDED,
            "Provider status confirmed success",
            TransitionEvidence.PROVIDER_STATUS,
            attempt_id=attempt.id,
        )
    assert _payment_state(payment_id) == "SUCCEEDED"

    transaction = get_provider_transaction(SessionLocal, attempt.id)
    assert transaction is not None
    assert transaction.amount_minor == 4312
    assert transaction.currency == "INR"
    assert transaction.status_query_count == 1


def test_timeout_stays_unknown_when_provider_has_no_resolution() -> None:
    payment_id = _create_payment()

    attempt = process_payment(SessionLocal, payment_id, ProviderScenario.TIMEOUT)

    assert attempt.status == "UNKNOWN"
    assert _payment_state(payment_id) == "UNKNOWN"
    result = get_provider_status(SessionLocal, attempt.id)
    assert result is not None
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.accepted is None
    assert _payment_state(payment_id) == "UNKNOWN"


def test_unknown_payment_cannot_be_resolved_without_authoritative_evidence() -> None:
    payment_id = _create_payment()
    attempt = process_payment(SessionLocal, payment_id, ProviderScenario.TIMEOUT)

    with SessionLocal() as session:
        with pytest.raises(InvalidStateTransition):
            transition_payment(
                session,
                payment_id,
                PaymentState.SUCCEEDED,
                "Unverified success",
                TransitionEvidence.PROVIDER_RESPONSE,
                attempt_id=attempt.id,
            )
    with SessionLocal() as session:
        with pytest.raises(InvalidStateTransition):
            transition_payment(
                session,
                payment_id,
                PaymentState.SUCCEEDED,
                "Provider status is still unknown",
                TransitionEvidence.PROVIDER_STATUS,
                attempt_id=attempt.id,
            )

    assert _payment_state(payment_id) == "UNKNOWN"


def test_temporary_unavailability_is_definitive_nonacceptance() -> None:
    payment_id = _create_payment()

    attempt = process_payment(
        SessionLocal,
        payment_id,
        ProviderScenario.TEMPORARY_UNAVAILABLE,
    )

    assert attempt.status == "NOT_ACCEPTED"
    assert attempt.error_code == "provider_temporarily_unavailable"
    assert _payment_state(payment_id) == "FAILED"
    result = get_provider_status(SessionLocal, attempt.id)
    assert result is not None
    assert result.outcome is ProviderOutcome.NOT_ACCEPTED
    assert result.accepted is False


def test_unknown_attempt_is_not_failed_over_to_another_provider() -> None:
    payment_id = _create_payment()
    first_attempt = process_payment(
        SessionLocal,
        payment_id,
        ProviderScenario.TIMEOUT,
    )

    with pytest.raises(InvalidStateTransition):
        process_payment(
            SessionLocal,
            payment_id,
            provider_name="provider_b",
        )

    with SessionLocal() as session:
        attempts = session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        ).all()
        provider_name = session.scalar(
            select(Provider.name).where(Provider.id == first_attempt.provider_id)
        )
    assert len(attempts) == 1
    assert provider_name == "provider_a"


def test_provider_b_is_used_only_when_explicitly_selected_for_first_attempt() -> None:
    payment_id = _create_payment()

    attempt = process_payment(
        SessionLocal,
        payment_id,
        provider_name="provider_b",
    )

    with SessionLocal() as session:
        provider_name = session.scalar(
            select(Provider.name).where(Provider.id == attempt.provider_id)
        )
    assert provider_name == "provider_b"


def test_terminal_state_cannot_be_regressed_by_late_transition() -> None:
    payment_id = _create_payment()
    attempt = process_payment(SessionLocal, payment_id)

    with SessionLocal() as session:
        with pytest.raises(InvalidStateTransition):
            transition_payment(
                session,
                payment_id,
                PaymentState.FAILED,
                "Late contradictory provider result",
                TransitionEvidence.PROVIDER_RESPONSE,
                attempt_id=attempt.id,
            )

    assert _payment_state(payment_id) == "SUCCEEDED"
    with SessionLocal() as session:
        assert session.scalar(
            select(PaymentAttempt.status).where(PaymentAttempt.id == attempt.id)
        ) == "SUCCEEDED"
        assert session.scalar(
            select(ProviderSimulationTransaction.id).where(
                ProviderSimulationTransaction.provider_reference
                == attempt.provider_reference
            )
        ) is not None


def test_state_transition_requires_attempt_belonging_to_payment() -> None:
    payment_id = _create_payment()
    other_payment_id = _create_payment()
    other_attempt = process_payment(SessionLocal, other_payment_id)

    with SessionLocal() as session:
        with pytest.raises(InvalidStateTransition):
            transition_payment(
                session,
                payment_id,
                PaymentState.PROCESSING,
                "Attempt belongs to another payment",
                TransitionEvidence.ATTEMPT_STARTED,
                attempt_id=other_attempt.id,
            )

    assert _payment_state(payment_id) == "CREATED"


def test_current_payment_states_match_contiguous_transition_histories() -> None:
    with SessionLocal() as session:
        has_payments = session.scalar(select(PaymentIntent.id).limit(1)) is not None
    if not has_payments:
        payment_id = _create_payment()
        process_payment(SessionLocal, payment_id)

    with SessionLocal() as session:
        payments = {
            payment.id: payment
            for payment in session.scalars(select(PaymentIntent)).all()
        }
        transitions = session.scalars(
            select(StateTransition).order_by(
                StateTransition.payment_id,
                StateTransition.occurred_at,
                StateTransition.id,
            )
        ).all()

    history_by_payment: dict[uuid.UUID, list[StateTransition]] = {}
    for transition in transitions:
        history_by_payment.setdefault(transition.payment_id, []).append(transition)

    assert payments
    assert history_by_payment.keys() == payments.keys()
    for payment_id, payment in payments.items():
        history = history_by_payment[payment_id]
        assert history[0].from_state is None
        assert history[0].to_state == PaymentState.CREATED.value
        for previous, current in zip(history, history[1:]):
            assert current.from_state == previous.to_state
        assert history[-1].to_state == payment.current_state
