import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.durable_job import DurableJob
from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.providers.base import ProviderScenario
from app.services.durable_jobs import claim_next_job
from app.services.job_execution import execute_claimed_job
from app.services.payment_orchestration import process_payment


def _create_payment(
    monkeypatch,
    scenario: ProviderScenario = ProviderScenario.SUCCESS,
) -> uuid.UUID:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.api.routes import simulation

    monkeypatch.setattr(
        simulation,
        "get_settings",
        lambda: SimpleNamespace(
            simulation_admin_token="test-admin-token",
            provider_callback_secret="test-callback-secret",
        ),
    )
    payload = {
        "merchant_id": f"phase4-merchant-{uuid.uuid4()}",
        "amount_minor": 4312,
        "currency": "INR",
        "idempotency_key": f"phase4-request-{uuid.uuid4()}",
        "scenario": scenario.value,
        "provider_name": "provider_a",
    }
    with TestClient(app) as client:
        response = client.post(
            "/simulation/payments",
            json=payload,
            headers={"X-Simulation-Admin-Token": "test-admin-token"},
        )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _job_id(payment_id: uuid.UUID, job_type: str) -> uuid.UUID:
    with SessionLocal() as session:
        job_id = session.scalar(
            select(DurableJob.id).where(
                DurableJob.payment_id == payment_id,
                DurableJob.job_type == job_type,
            )
        )
    assert job_id is not None
    return job_id


def _run_job(job_id: uuid.UUID, worker_id: str = "phase4-test-worker") -> None:
    claimed_id = claim_next_job(
        SessionLocal,
        worker_id,
        job_id=job_id,
    )
    assert claimed_id == job_id
    execute_claimed_job(SessionLocal, job_id, worker_id)


def test_worker_executes_durable_payment_and_reconciliation_jobs(monkeypatch) -> None:
    payment_id = _create_payment(monkeypatch, ProviderScenario.SUCCESS_LOST_RESPONSE)
    payment_job_id = _job_id(payment_id, "PROCESS_PAYMENT")

    _run_job(payment_job_id)

    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        payment_job = session.get(DurableJob, payment_job_id)
        attempt = session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
    assert payment is not None and payment.current_state == "UNKNOWN"
    assert payment_job is not None and payment_job.status == "COMPLETED"
    assert attempt is not None and attempt.status == "UNKNOWN"

    reconciliation_job_id = _job_id(payment_id, "RECONCILE_PAYMENT")
    _run_job(reconciliation_job_id, "phase4-reconciler")

    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        reconciliation_job = session.get(DurableJob, reconciliation_job_id)
        provider_transaction = session.scalar(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.payment_id == payment_id
            )
        )
    assert payment is not None and payment.current_state == "SUCCEEDED"
    assert reconciliation_job is not None
    assert reconciliation_job.status == "COMPLETED"
    assert provider_transaction is not None
    assert provider_transaction.status_query_count == 1


def test_unknown_reconciliation_stays_unknown_and_retries_are_bounded(
    monkeypatch,
) -> None:
    payment_id = _create_payment(monkeypatch, ProviderScenario.TIMEOUT)
    _run_job(_job_id(payment_id, "PROCESS_PAYMENT"))
    reconciliation_job_id = _job_id(payment_id, "RECONCILE_PAYMENT")

    with SessionLocal() as session, session.begin():
        job = session.get(DurableJob, reconciliation_job_id)
        assert job is not None
        job.max_retries = 1

    _run_job(reconciliation_job_id, "phase4-reconciler")
    with SessionLocal() as session, session.begin():
        job = session.get(DurableJob, reconciliation_job_id)
        assert job is not None
        assert job.status == "PENDING"
        assert job.retry_count == 1
        job.available_at = session.scalar(
            select(DurableJob.created_at).where(
                DurableJob.id == reconciliation_job_id
            )
        )

    _run_job(reconciliation_job_id, "phase4-reconciler")

    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        job = session.get(DurableJob, reconciliation_job_id)
    assert payment is not None and payment.current_state == "UNKNOWN"
    assert job is not None and job.status == "DEAD"
    assert job.retry_count == 2


def test_worker_lease_prevents_double_claim_and_recovers_expired_work(monkeypatch) -> None:
    payment_id = _create_payment(monkeypatch)
    job_id = _job_id(payment_id, "PROCESS_PAYMENT")

    claims: list[uuid.UUID | None]
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda worker_id: claim_next_job(
                    SessionLocal,
                    worker_id,
                    job_id=job_id,
                ),
                ["phase4-worker-a", "phase4-worker-b"],
            )
        )
    assert claims.count(job_id) == 1
    assert claims.count(None) == 1

    with SessionLocal() as session, session.begin():
        job = session.get(DurableJob, job_id)
        assert job is not None and job.status == "PROCESSING"
        job.lease_expires_at = session.scalar(
            select(DurableJob.created_at).where(DurableJob.id == job_id)
        )

    reclaimed_id = claim_next_job(
        SessionLocal,
        "phase4-recovery-worker",
        job_id=job_id,
    )
    assert reclaimed_id == job_id
    with SessionLocal() as session:
        job = session.get(DurableJob, job_id)
    assert job is not None
    assert job.lease_owner == "phase4-recovery-worker"


def test_nonretryable_job_failure_is_dead_lettered(monkeypatch) -> None:
    payment_id = _create_payment(monkeypatch)
    attempt = process_payment(
        SessionLocal,
        payment_id,
        scenario=ProviderScenario.TIMEOUT,
        provider_name="provider_b",
    )
    job_id = _job_id(payment_id, "PROCESS_PAYMENT")

    _run_job(job_id, "phase4-invalid-job-worker")

    with SessionLocal() as session:
        job = session.get(DurableJob, job_id)
    assert attempt.status == "UNKNOWN"
    assert job is not None
    assert job.status == "DEAD"
    assert job.retry_count == 0
