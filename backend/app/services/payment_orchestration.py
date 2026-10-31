import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider import Provider
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.providers.base import (
    ProviderAdapter,
    ProviderOutcome,
    ProviderResult,
    ProviderScenario,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.simulated import ProviderA, ProviderB
from app.schemas.payment import PaymentState
from app.services.state_machine import (
    InvalidStateTransition,
    PaymentNotFound,
    TransitionEvidence,
    transition_payment_in_transaction,
)


_SUPPORTED_PROVIDERS = {"provider_a", "provider_b"}


def _provider_id(session: Session, name: str) -> uuid.UUID:
    provider_id = session.execute(
        insert(Provider)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[Provider.name])
        .returning(Provider.id)
    ).scalar_one_or_none()
    if provider_id is None:
        provider_id = session.scalar(select(Provider.id).where(Provider.name == name))
    if provider_id is None:
        raise RuntimeError(f"Provider {name} could not be registered")
    return provider_id


def _provider_adapters(
    session_factory: sessionmaker[Session],
) -> dict[str, ProviderAdapter]:
    return {
        ProviderA.name: ProviderA(session_factory),
        ProviderB.name: ProviderB(session_factory),
    }


def process_payment(
    session_factory: sessionmaker[Session],
    payment_id: uuid.UUID,
    scenario: ProviderScenario = ProviderScenario.SUCCESS,
    provider_name: str | None = None,
) -> PaymentAttempt:
    selected_provider = provider_name or ProviderA.name
    if selected_provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported simulated provider: {selected_provider}")

    with session_factory() as session, session.begin():
        payment = session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.id == payment_id)
            .with_for_update()
        )
        if payment is None:
            raise PaymentNotFound()
        if payment.current_state != PaymentState.CREATED.value:
            raise InvalidStateTransition(
                "A new provider attempt can only start for a CREATED payment"
            )

        attempt_count = session.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.payment_id == payment_id)
        )
        if attempt_count:
            raise InvalidStateTransition(
                "A payment with a prior attempt cannot be routed to another provider"
            )

        provider_id = _provider_id(session, selected_provider)
        attempt_id = uuid.uuid4()
        attempt_number = 1
        provider_reference = f"{payment_id}:{attempt_number}"
        attempt = PaymentAttempt(
            id=attempt_id,
            payment_id=payment_id,
            provider_id=provider_id,
            attempt_number=attempt_number,
            provider_reference=provider_reference,
            status="IN_PROGRESS",
            reason=f"Initial attempt routed to {selected_provider}",
        )
        session.add(attempt)
        session.flush()
        transition_payment_in_transaction(
            session,
            payment,
            PaymentState.PROCESSING,
            "Provider attempt started",
            TransitionEvidence.ATTEMPT_STARTED,
            attempt_id=attempt_id,
        )
        amount_minor = payment.amount_minor
        currency = payment.currency

    adapter = _provider_adapters(session_factory)[selected_provider]
    try:
        result = adapter.submit(
            payment_id,
            amount_minor,
            currency,
            provider_reference,
            scenario,
        )
    except ProviderTimeout as error:
        _record_provider_outcome(
            session_factory,
            payment_id,
            attempt_id,
            ProviderOutcome.UNKNOWN,
            error.code,
            error.safe_message,
        )
    except ProviderUnavailable as error:
        _record_provider_outcome(
            session_factory,
            payment_id,
            attempt_id,
            ProviderOutcome.NOT_ACCEPTED,
            error.code,
            error.safe_message,
        )
    else:
        _record_provider_outcome(
            session_factory,
            payment_id,
            attempt_id,
            result.outcome,
            None,
            None,
        )

    with session_factory() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        if attempt is None:
            raise RuntimeError("Provider attempt disappeared after processing")
        return attempt


def _record_provider_outcome(
    session_factory: sessionmaker[Session],
    payment_id: uuid.UUID,
    attempt_id: uuid.UUID,
    outcome: ProviderOutcome | None,
    error_code: str | None,
    error_message: str | None,
) -> None:
    with session_factory() as session, session.begin():
        payment = session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.id == payment_id)
            .with_for_update()
        )
        attempt = session.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.id == attempt_id)
            .with_for_update()
        )
        if payment is None or attempt is None or attempt.payment_id != payment_id:
            raise PaymentNotFound()

        attempt.finished_at = func.now()
        attempt.error_code = error_code
        attempt.error_message = error_message

        outcome_details = {
            ProviderOutcome.UNKNOWN: (
                "UNKNOWN",
                PaymentState.UNKNOWN,
                TransitionEvidence.PROVIDER_TIMEOUT,
                "Provider outcome is uncertain; reconciliation is required",
            ),
            ProviderOutcome.SUCCEEDED: (
                "SUCCEEDED",
                PaymentState.SUCCEEDED,
                TransitionEvidence.PROVIDER_RESPONSE,
                "Provider confirmed success",
            ),
            ProviderOutcome.DECLINED: (
                "DECLINED",
                PaymentState.FAILED,
                TransitionEvidence.PROVIDER_RESPONSE,
                "Provider returned a definitive decline",
            ),
            ProviderOutcome.NOT_ACCEPTED: (
                "RETRYABLE_FAILURE",
                None,
                None,
                "Provider was unavailable before accepting the request",
            ),
        }
        details = outcome_details.get(outcome)
        if details is None:
            raise InvalidStateTransition("Provider returned an unsupported outcome")
        attempt.status, target_state, evidence, reason = details

        if target_state is not None:
            if payment.current_state == PaymentState.PROCESSING.value:
                if evidence is None:
                    raise RuntimeError("Provider outcome is missing transition evidence")
                transition_payment_in_transaction(
                    session,
                    payment,
                    target_state,
                    reason,
                    evidence,
                    attempt_id=attempt_id,
                )
            else:
                attempt.status = "LATE_RESULT_IGNORED"
                attempt.error_code = "late_provider_result"
                attempt.error_message = (
                    "The payment was already terminal when the provider result arrived."
                )


def get_provider_status(
    session_factory: sessionmaker[Session],
    attempt_id: uuid.UUID,
) -> ProviderResult | None:
    with session_factory() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        if attempt is None:
            raise PaymentNotFound()
        provider_name = session.scalar(
            select(Provider.name).where(Provider.id == attempt.provider_id)
        )
        provider_reference = attempt.provider_reference
        if provider_name is None or provider_reference is None:
            return None
        if provider_name not in _SUPPORTED_PROVIDERS:
            return None

    adapter = _provider_adapters(session_factory)[provider_name]
    return adapter.get_status(provider_reference)


def get_provider_transaction(
    session_factory: sessionmaker[Session],
    attempt_id: uuid.UUID,
) -> ProviderSimulationTransaction | None:
    with session_factory() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        if attempt is None:
            raise PaymentNotFound()
        return session.scalar(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.provider_id == attempt.provider_id,
                ProviderSimulationTransaction.provider_reference
                == attempt.provider_reference,
            )
        )
