import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.provider import Provider
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.providers.base import (
    ProviderOutcome,
    ProviderReferenceConflict,
    ProviderResult,
    ProviderScenario,
    ProviderTimeout,
    ProviderUnavailable,
)


_SCENARIO_OUTCOMES = {
    ProviderScenario.SUCCESS: (ProviderOutcome.SUCCEEDED, True),
    ProviderScenario.DECLINE: (ProviderOutcome.DECLINED, True),
    ProviderScenario.TIMEOUT: (ProviderOutcome.UNKNOWN, None),
    ProviderScenario.SUCCESS_LOST_RESPONSE: (ProviderOutcome.SUCCEEDED, True),
    ProviderScenario.TEMPORARY_UNAVAILABLE: (ProviderOutcome.NOT_ACCEPTED, False),
}


class _DatabaseSimulatedProvider:
    name: str

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def submit(
        self,
        payment_id: uuid.UUID,
        amount_minor: int,
        currency: str,
        provider_reference: str,
        scenario: ProviderScenario,
    ) -> ProviderResult:
        with self._session_factory() as session, session.begin():
            provider_id = session.scalar(
                select(Provider.id).where(Provider.name == self.name)
            )
            if provider_id is None:
                raise RuntimeError(f"Provider {self.name} has not been registered")

            transaction = session.scalar(
                select(ProviderSimulationTransaction)
                .where(
                    ProviderSimulationTransaction.provider_id == provider_id,
                    ProviderSimulationTransaction.provider_reference
                    == provider_reference,
                )
                .with_for_update()
            )
            if transaction is None:
                outcome, accepted = _SCENARIO_OUTCOMES[scenario]
                transaction = ProviderSimulationTransaction(
                    payment_id=payment_id,
                    provider_id=provider_id,
                    amount_minor=amount_minor,
                    currency=currency,
                    provider_reference=provider_reference,
                    scenario=scenario.value,
                    outcome=outcome.value,
                    accepted=accepted,
                )
                session.add(transaction)
                session.flush()
            elif (
                transaction.payment_id != payment_id
                or transaction.amount_minor != amount_minor
                or transaction.currency != currency
                or transaction.scenario != scenario.value
            ):
                raise ProviderReferenceConflict()

            result = ProviderResult(
                provider_reference=transaction.provider_reference,
                outcome=ProviderOutcome(transaction.outcome),
                accepted=transaction.accepted,
            )

        if scenario in (
            ProviderScenario.TIMEOUT,
            ProviderScenario.SUCCESS_LOST_RESPONSE,
        ):
            raise ProviderTimeout()
        if scenario is ProviderScenario.TEMPORARY_UNAVAILABLE:
            raise ProviderUnavailable()
        return result

    def get_status(self, provider_reference: str) -> ProviderResult | None:
        with self._session_factory() as session, session.begin():
            provider_id = session.scalar(
                select(Provider.id).where(Provider.name == self.name)
            )
            if provider_id is None:
                return None

            transaction = session.scalar(
                select(ProviderSimulationTransaction)
                .where(
                    ProviderSimulationTransaction.provider_id == provider_id,
                    ProviderSimulationTransaction.provider_reference
                    == provider_reference,
                )
                .with_for_update()
            )
            if transaction is None:
                return None
            transaction.status_query_count += 1
            result = ProviderResult(
                provider_reference=transaction.provider_reference,
                outcome=ProviderOutcome(transaction.outcome),
                accepted=transaction.accepted,
            )
        return result


class ProviderA(_DatabaseSimulatedProvider):
    name = "provider_a"


class ProviderB(_DatabaseSimulatedProvider):
    name = "provider_b"
