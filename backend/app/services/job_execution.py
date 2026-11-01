import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.durable_job import DurableJob
from app.providers.base import ProviderScenario
from app.services.durable_jobs import complete_job, retry_job
from app.services.payment_orchestration import process_payment, reconcile_attempt
from app.services.state_machine import InvalidStateTransition, PaymentNotFound

logger = logging.getLogger("payment_orchestrator.jobs")


def execute_claimed_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    worker_id: str,
) -> None:
    with session_factory() as session:
        job = session.scalar(
            select(DurableJob).where(
                DurableJob.id == job_id,
                DurableJob.status == "PROCESSING",
                DurableJob.lease_owner == worker_id,
            )
        )
        if job is None:
            raise RuntimeError("Claimed job is missing or its lease was lost")
        job_type = job.job_type
        payment_id = job.payment_id
        attempt_id = job.attempt_id
        provider_name = job.provider_name
        scenario_value = job.scenario

    try:
        scenario = ProviderScenario(scenario_value)
        if job_type == "PROCESS_PAYMENT":
            process_payment(
                session_factory,
                payment_id,
                scenario=scenario,
                provider_name=provider_name,
            )
        elif job_type == "RECONCILE_PAYMENT":
            if attempt_id is None:
                raise ValueError("A reconciliation job must reference an attempt")
            if not reconcile_attempt(session_factory, attempt_id):
                retry_job(
                    session_factory,
                    job_id,
                    worker_id,
                    "Provider outcome remains uncertain",
                )
                return
        else:
            raise ValueError(f"Unsupported durable job type: {job_type}")
    except Exception as error:
        logger.exception("Durable job %s failed", job_id)
        retry_job(
            session_factory,
            job_id,
            worker_id,
            f"Job failed with {type(error).__name__}",
            retryable=not isinstance(
                error,
                (InvalidStateTransition, PaymentNotFound, ValueError),
            ),
        )
        return

    complete_job(session_factory, job_id, worker_id)
