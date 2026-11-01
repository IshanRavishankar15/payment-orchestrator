import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.durable_job import DurableJob


class JobLeaseLost(Exception):
    pass


def claim_next_job(
    session_factory: sessionmaker[Session],
    worker_id: str,
    lease_seconds: int = 120,
    job_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    with session_factory() as session, session.begin():
        statement = select(DurableJob).where(
            or_(
                (DurableJob.status == "PENDING")
                & (DurableJob.available_at <= func.now()),
                (DurableJob.status == "PROCESSING")
                & (DurableJob.lease_expires_at <= func.now()),
            )
        )
        if job_id is not None:
            statement = statement.where(DurableJob.id == job_id)
        job = session.scalar(
            statement.order_by(
                DurableJob.available_at, DurableJob.created_at, DurableJob.id
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "PROCESSING"
        job.lease_owner = worker_id
        job.lease_expires_at = func.now() + timedelta(seconds=lease_seconds)
        return job.id


def complete_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    worker_id: str,
) -> None:
    with session_factory() as session, session.begin():
        job = session.scalar(
            select(DurableJob).where(DurableJob.id == job_id).with_for_update()
        )
        if job is None or job.status != "PROCESSING" or job.lease_owner != worker_id:
            raise JobLeaseLost("The worker no longer owns this job lease")
        job.status = "COMPLETED"
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = None


def retry_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    worker_id: str,
    safe_error: str,
    retryable: bool = True,
) -> None:
    with session_factory() as session, session.begin():
        job = session.scalar(
            select(DurableJob).where(DurableJob.id == job_id).with_for_update()
        )
        if job is None or job.status != "PROCESSING" or job.lease_owner != worker_id:
            raise JobLeaseLost("The worker no longer owns this job lease")

        job.last_error = safe_error[:500]
        job.lease_owner = None
        job.lease_expires_at = None
        if not retryable:
            job.status = "DEAD"
            return

        job.retry_count += 1
        if job.retry_count > job.max_retries:
            job.status = "DEAD"
            return

        job.status = "PENDING"
        job.available_at = func.now() + timedelta(
            seconds=min(2 ** (job.retry_count - 1), 60)
        )
