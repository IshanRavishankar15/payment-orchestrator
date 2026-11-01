import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models.durable_job import DurableJob
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

    should_submit = False
    with session_factory() as session, session.begin():
        payment = session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.id == payment_id)
            .with_for_update()
        )
        if payment is None:
            raise PaymentNotFound()

        if payment.current_state == PaymentState.CREATED.value:
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
            should_submit = True
        else:
            attempt = session.scalar(
                select(PaymentAttempt)
                .where(PaymentAttempt.payment_id == payment_id)
                .order_by(PaymentAttempt.attempt_number)
                .limit(1)
            )
            if attempt is None:
                raise InvalidStateTransition(
                    "The payment has no provider attempt to resume"
                )
            provider_name_for_attempt = session.scalar(
                select(Provider.name).where(Provider.id == attempt.provider_id)
            )
            if provider_name_for_attempt is None:
                raise RuntimeError("The provider for the existing attempt is missing")
            if (
                provider_name is not None
                and provider_name != provider_name_for_attempt
            ):
                raise InvalidStateTransition(
                    "A payment cannot be routed to a different provider after an attempt"
                )
            selected_provider = provider_name_for_attempt

            if payment.current_state == PaymentState.PROCESSING.value:
                if attempt.status != "IN_PROGRESS":
                    raise InvalidStateTransition(
                        "A processing payment must have an in-progress attempt"
                    )
                should_submit = True
            elif payment.current_state == PaymentState.UNKNOWN.value:
                if attempt.status != "UNKNOWN":
                    raise InvalidStateTransition(
                        "An unknown payment must have an uncertain provider attempt"
                    )
                session.execute(
                    insert(DurableJob)
                    .values(
                        job_type="RECONCILE_PAYMENT",
                        payment_id=payment_id,
                        attempt_id=attempt.id,
                        provider_name=selected_provider,
                        scenario=scenario.value,
                        dedupe_key=f"reconcile:{attempt.id}",
                    )
                    .on_conflict_do_nothing(
                        index_elements=[DurableJob.dedupe_key]
                    )
                )
            elif payment.current_state in (
                PaymentState.SUCCEEDED.value,
                PaymentState.FAILED.value,
            ):
                return attempt
            else:
                raise InvalidStateTransition("The payment cannot be processed")

        attempt_id = attempt.id
        provider_reference = attempt.provider_reference
        if provider_reference is None:
            raise InvalidStateTransition("The provider attempt has no reference")
        amount_minor = payment.amount_minor
        currency = payment.currency

    if not should_submit:
        with session_factory() as session:
            resumed_attempt = session.get(PaymentAttempt, attempt_id)
            if resumed_attempt is None:
                raise RuntimeError("Provider attempt disappeared after processing")
            return resumed_attempt

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
        completed_attempt = session.get(PaymentAttempt, attempt_id)
        if completed_attempt is None:
            raise RuntimeError("Provider attempt disappeared after processing")
        return completed_attempt


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
                "NOT_ACCEPTED",
                PaymentState.FAILED,
                TransitionEvidence.PROVIDER_RESPONSE,
                "Provider definitively did not accept the request",
            ),
        }
        details = outcome_details.get(outcome)
        if details is None:
            raise InvalidStateTransition("Provider returned an unsupported outcome")
        attempt_status, target_state, evidence, reason = details

        if target_state is PaymentState.UNKNOWN:
            if payment.current_state == PaymentState.PROCESSING.value:
                attempt.status = attempt_status
                attempt.finished_at = func.now()
                attempt.error_code = error_code
                attempt.error_message = error_message
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
            elif payment.current_state == PaymentState.UNKNOWN.value:
                attempt.status = attempt_status
                attempt.finished_at = func.now()
                attempt.error_code = error_code
                attempt.error_message = error_message

            if payment.current_state == PaymentState.UNKNOWN.value:
                session.execute(
                    insert(DurableJob)
                    .values(
                        job_type="RECONCILE_PAYMENT",
                        payment_id=payment_id,
                        attempt_id=attempt_id,
                        provider_name=session.scalar(
                            select(Provider.name).where(Provider.id == attempt.provider_id)
                        ),
                        dedupe_key=f"reconcile:{attempt_id}",
                    )
                    .on_conflict_do_nothing(
                        index_elements=[DurableJob.dedupe_key]
                    )
                )
            return

        if target_state is not None:
            attempt.finished_at = func.now()
            attempt.error_code = error_code
            attempt.error_message = error_message
            if payment.current_state == PaymentState.PROCESSING.value:
                attempt.status = attempt_status
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
            elif payment.current_state == PaymentState.UNKNOWN.value:
                attempt.status = attempt_status
                transition_payment_in_transaction(
                    session,
                    payment,
                    target_state,
                    reason,
                    TransitionEvidence.PROVIDER_STATUS,
                    attempt_id=attempt_id,
                )
            elif payment.current_state == target_state.value:
                attempt.status = attempt_status
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


def reconcile_attempt(
    session_factory: sessionmaker[Session],
    attempt_id: uuid.UUID,
) -> bool:
    with session_factory() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        if attempt is None:
            raise PaymentNotFound()
        payment = session.get(PaymentIntent, attempt.payment_id)
        if payment is None:
            raise PaymentNotFound()
        if payment.current_state in (
            PaymentState.SUCCEEDED.value,
            PaymentState.FAILED.value,
        ):
            return True

    result = get_provider_status(session_factory, attempt_id)
    if result is None or result.outcome is ProviderOutcome.UNKNOWN:
        return False
    payment_id = attempt.payment_id

    _record_provider_outcome(
        session_factory,
        payment_id,
        attempt_id,
        result.outcome,
        None,
        None,
    )
    with session_factory() as session:
        payment = session.get(PaymentIntent, payment_id)
        if payment is None:
            raise PaymentNotFound()
        return payment.current_state in (
            PaymentState.SUCCEEDED.value,
            PaymentState.FAILED.value,
        )
