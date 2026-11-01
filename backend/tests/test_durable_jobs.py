import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.main import app
from app.models.durable_job import DurableJob
from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider_simulation_transaction import ProviderSimulationTransaction
from app.models.state_transition import StateTransition
from app.providers.base import ProviderScenario
from app.providers.simulated import ProviderA
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
            provider_a_callback_secret="test-provider-a-secret",
            provider_b_callback_secret="test-provider-b-secret",
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


@pytest.mark.parametrize(
    ("scenario", "expected_state", "expected_attempt_status"),
    [
        (ProviderScenario.SUCCESS, "SUCCEEDED", "SUCCEEDED"),
        (ProviderScenario.DECLINE, "FAILED", "DECLINED"),
        (ProviderScenario.TIMEOUT, "UNKNOWN", "UNKNOWN"),
        (ProviderScenario.TEMPORARY_UNAVAILABLE, "FAILED", "NOT_ACCEPTED"),
    ],
)
def test_worker_scenarios_are_visible_in_payment_timeline(
    monkeypatch,
    scenario: ProviderScenario,
    expected_state: str,
    expected_attempt_status: str,
) -> None:
    from fastapi.testclient import TestClient

    payment_id = _create_payment(monkeypatch, scenario)
    _run_job(_job_id(payment_id, "PROCESS_PAYMENT"))

    with TestClient(app) as client:
        response = client.get(f"/payments/{payment_id}/timeline")

    assert response.status_code == 200, response.text
    timeline = response.json()
    assert timeline["payment"]["state"] == expected_state
    assert [attempt["status"] for attempt in timeline["attempts"]] == [
        expected_attempt_status
    ]
    assert [transition["to_state"] for transition in timeline["transitions"]] == [
        "CREATED",
        "PROCESSING",
        expected_state,
    ]
    with SessionLocal() as session:
        payment_job = session.get(
            DurableJob,
            _job_id(payment_id, "PROCESS_PAYMENT"),
        )
    assert payment_job is not None
    assert payment_job.status == "COMPLETED"
    assert payment_job.retry_count == 0
    if expected_state == "UNKNOWN":
        assert len(timeline["reconciliation_jobs"]) == 1
        assert timeline["reconciliation_jobs"][0]["status"] == "PENDING"
    else:
        assert timeline["reconciliation_jobs"] == []


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
        assert job.available_at > session.scalar(select(func.now()))
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

    execute_claimed_job(SessionLocal, job_id, "phase4-recovery-worker")
    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        job = session.get(DurableJob, job_id)
        attempts = session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        ).all()
    assert payment is not None and payment.current_state == "SUCCEEDED"
    assert job is not None and job.status == "COMPLETED"
    assert len(attempts) == 1


def test_worker_recovers_after_provider_commit_before_result_persistence(
    monkeypatch,
) -> None:
    payment_id = _create_payment(monkeypatch)
    job_id = _job_id(payment_id, "PROCESS_PAYMENT")
    first_worker = "phase6-interrupted-provider-worker"
    assert claim_next_job(SessionLocal, first_worker, job_id=job_id) == job_id
    original_submit = ProviderA.submit

    def submit_then_interrupt(*args, **kwargs):
        original_submit(*args, **kwargs)
        raise SystemExit("Simulated worker interruption after provider submission")

    monkeypatch.setattr(ProviderA, "submit", submit_then_interrupt)
    with pytest.raises(SystemExit):
        execute_claimed_job(SessionLocal, job_id, first_worker)

    with SessionLocal() as session, session.begin():
        job = session.get(DurableJob, job_id)
        assert job is not None and job.status == "PROCESSING"
        job.lease_expires_at = session.scalar(
            select(DurableJob.created_at).where(DurableJob.id == job_id)
        )
        attempt = session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        provider_transaction_count = session.scalar(
            select(func.count())
            .select_from(ProviderSimulationTransaction)
            .where(ProviderSimulationTransaction.payment_id == payment_id)
        )
    assert attempt is not None and attempt.status == "IN_PROGRESS"
    assert provider_transaction_count == 1

    monkeypatch.setattr(ProviderA, "submit", original_submit)
    recovery_worker = "phase6-provider-recovery-worker"
    assert claim_next_job(SessionLocal, recovery_worker, job_id=job_id) == job_id
    execute_claimed_job(SessionLocal, job_id, recovery_worker)

    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        job = session.get(DurableJob, job_id)
        attempts = session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        ).all()
        transactions = session.scalars(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.payment_id == payment_id
            )
        ).all()
        transitions = session.scalars(
            select(StateTransition)
            .where(StateTransition.payment_id == payment_id)
            .order_by(StateTransition.occurred_at, StateTransition.id)
        ).all()
    assert payment is not None and payment.current_state == "SUCCEEDED"
    assert job is not None and job.status == "COMPLETED"
    assert len(attempts) == 1
    assert len(transactions) == 1
    assert [transition.to_state for transition in transitions] == [
        "CREATED",
        "PROCESSING",
        "SUCCEEDED",
    ]


def test_worker_recovers_after_payment_commit_before_job_acknowledgement(
    monkeypatch,
) -> None:
    import app.services.job_execution as job_execution

    payment_id = _create_payment(monkeypatch)
    job_id = _job_id(payment_id, "PROCESS_PAYMENT")
    first_worker = "phase6-interrupted-ack-worker"
    assert claim_next_job(SessionLocal, first_worker, job_id=job_id) == job_id
    original_complete = job_execution.complete_job

    def interrupt_before_acknowledgement(*_args, **_kwargs):
        raise SystemExit("Simulated worker interruption before acknowledgement")

    monkeypatch.setattr(
        job_execution, "complete_job", interrupt_before_acknowledgement
    )
    with pytest.raises(SystemExit):
        execute_claimed_job(SessionLocal, job_id, first_worker)

    with SessionLocal() as session, session.begin():
        job = session.get(DurableJob, job_id)
        payment = session.get(PaymentIntent, payment_id)
        assert job is not None and job.status == "PROCESSING"
        assert payment is not None and payment.current_state == "SUCCEEDED"
        job.lease_expires_at = session.scalar(
            select(DurableJob.created_at).where(DurableJob.id == job_id)
        )

    monkeypatch.setattr(job_execution, "complete_job", original_complete)
    recovery_worker = "phase6-ack-recovery-worker"
    assert claim_next_job(SessionLocal, recovery_worker, job_id=job_id) == job_id
    execute_claimed_job(SessionLocal, job_id, recovery_worker)

    with SessionLocal() as session:
        job = session.get(DurableJob, job_id)
        attempts = session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        ).all()
        transactions = session.scalars(
            select(ProviderSimulationTransaction).where(
                ProviderSimulationTransaction.payment_id == payment_id
            )
        ).all()
    assert job is not None and job.status == "COMPLETED"
    assert len(attempts) == 1
    assert len(transactions) == 1


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
