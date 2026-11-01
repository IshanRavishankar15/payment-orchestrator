import hashlib
import hmac
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routes import simulation
from app.db.session import SessionLocal
from app.main import app
from app.models.durable_job import DurableJob
from app.models.payment_attempt import PaymentAttempt
from app.models.payment_intent import PaymentIntent
from app.models.provider_event import ProviderEvent
from app.providers.base import ProviderScenario
from app.services.durable_jobs import claim_next_job
from app.services.job_execution import execute_claimed_job
from app.services.provider_callbacks import process_provider_callback


CALLBACK_SECRET = "test-callback-secret"
ADMIN_TOKEN = "test-admin-token"


def _configure_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        simulation,
        "get_settings",
        lambda: SimpleNamespace(
            simulation_admin_token=ADMIN_TOKEN,
            provider_callback_secret=CALLBACK_SECRET,
        ),
    )


def _create_unknown_payment(monkeypatch) -> tuple[uuid.UUID, str]:
    _configure_auth(monkeypatch)
    payload = {
        "merchant_id": f"phase4-callback-merchant-{uuid.uuid4()}",
        "amount_minor": 4312,
        "currency": "INR",
        "idempotency_key": f"phase4-callback-request-{uuid.uuid4()}",
        "scenario": ProviderScenario.SUCCESS_LOST_RESPONSE.value,
        "provider_name": "provider_a",
    }
    with TestClient(app) as client:
        response = client.post(
            "/simulation/payments",
            json=payload,
            headers={"X-Simulation-Admin-Token": ADMIN_TOKEN},
        )
    assert response.status_code == 201, response.text
    payment_id = uuid.UUID(response.json()["id"])

    with SessionLocal() as session:
        job_id = session.scalar(
            select(DurableJob.id).where(
                DurableJob.payment_id == payment_id,
                DurableJob.job_type == "PROCESS_PAYMENT",
            )
        )
    assert job_id is not None
    worker_id = f"callback-test-{uuid.uuid4()}"
    assert claim_next_job(SessionLocal, worker_id, job_id=job_id) == job_id
    execute_claimed_job(SessionLocal, job_id, worker_id)

    with SessionLocal() as session:
        attempt = session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
    assert attempt is not None and attempt.provider_reference is not None
    return payment_id, attempt.provider_reference


def _signed_callback(payload: dict[str, str]) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        CALLBACK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return body, f"sha256={signature}"


def test_callbacks_are_authenticated_deduplicated_and_cannot_contradict_provider(
    monkeypatch,
) -> None:
    payment_id, provider_reference = _create_unknown_payment(monkeypatch)
    payload = {
        "provider_event_id": f"callback-{uuid.uuid4()}",
        "event_type": "PAYMENT_SUCCEEDED",
        "provider_reference": provider_reference,
    }
    body, signature = _signed_callback(payload)

    with TestClient(app) as client:
        unauthorized = client.post(
            "/simulation/providers/provider_a/callbacks",
            content=body,
            headers={"X-Provider-Signature": "sha256=invalid"},
        )
        applied = client.post(
            "/simulation/providers/provider_a/callbacks",
            content=body,
            headers={"X-Provider-Signature": signature},
        )
        duplicate = client.post(
            "/simulation/providers/provider_a/callbacks",
            content=body,
            headers={"X-Provider-Signature": signature},
        )

        contradictory_payload = {
            **payload,
            "provider_event_id": f"contradictory-{uuid.uuid4()}",
            "event_type": "PAYMENT_DECLINED",
        }
        contradictory_body, contradictory_signature = _signed_callback(
            contradictory_payload
        )
        contradictory = client.post(
            "/simulation/providers/provider_a/callbacks",
            content=contradictory_body,
            headers={"X-Provider-Signature": contradictory_signature},
        )

    assert unauthorized.status_code == 401
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied_succeeded"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert contradictory.status_code == 200
    assert contradictory.json()["status"] == "contradictory"

    with SessionLocal() as session:
        payment = session.get(PaymentIntent, payment_id)
        event_count = session.scalar(
            select(func.count())
            .select_from(ProviderEvent)
            .where(
                ProviderEvent.provider_event_id.in_(
                    [
                        payload["provider_event_id"],
                        contradictory_payload["provider_event_id"],
                    ]
                )
            )
        )
        results = session.scalars(
            select(ProviderEvent.processing_result).where(
                ProviderEvent.provider_event_id.in_(
                    [
                        payload["provider_event_id"],
                        contradictory_payload["provider_event_id"],
                    ]
                )
            )
        ).all()
    assert payment is not None and payment.current_state == "SUCCEEDED"
    assert event_count == 2
    assert set(results) == {"APPLIED_SUCCEEDED", "CONTRADICTORY"}


def test_concurrent_duplicate_callback_is_applied_once(monkeypatch) -> None:
    _payment_id, provider_reference = _create_unknown_payment(monkeypatch)
    payload = {
        "provider_event_id": f"callback-race-{uuid.uuid4()}",
        "event_type": "PAYMENT_SUCCEEDED",
        "provider_reference": provider_reference,
    }
    body, signature = _signed_callback(payload)

    def deliver(_: int) -> str:
        with TestClient(app) as client:
            response = client.post(
                "/simulation/providers/provider_a/callbacks",
                content=body,
                headers={"X-Provider-Signature": signature},
            )
        assert response.status_code == 200, response.text
        return response.json()["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(deliver, range(2)))

    assert sorted(results) == ["applied_succeeded", "duplicate"]
    with SessionLocal() as session:
        event_count = session.scalar(
            select(func.count())
            .select_from(ProviderEvent)
            .where(ProviderEvent.provider_event_id == payload["provider_event_id"])
        )
    assert event_count == 1


def test_simulation_admin_controls_require_configured_token(monkeypatch) -> None:
    _configure_auth(monkeypatch)
    payload = {
        "merchant_id": f"phase4-admin-merchant-{uuid.uuid4()}",
        "amount_minor": 1,
        "currency": "INR",
        "idempotency_key": f"phase4-admin-request-{uuid.uuid4()}",
        "scenario": "SUCCESS",
    }
    with TestClient(app) as client:
        response = client.post("/simulation/payments", json=payload)

    assert response.status_code == 401


def test_payment_timeline_returns_live_attempt_event_and_reconciliation_data(
    monkeypatch,
) -> None:
    payment_id, provider_reference = _create_unknown_payment(monkeypatch)
    callback = {
        "provider_event_id": f"timeline-{uuid.uuid4()}",
        "event_type": "PAYMENT_SUCCEEDED",
        "provider_reference": provider_reference,
    }
    assert (
        process_provider_callback(SessionLocal, "provider_a", callback)
        == "APPLIED_SUCCEEDED"
    )

    with TestClient(app) as client:
        response = client.get(f"/payments/{payment_id}/timeline")

    assert response.status_code == 200, response.text
    timeline = response.json()
    assert timeline["payment"]["state"] == "SUCCEEDED"
    assert [attempt["status"] for attempt in timeline["attempts"]] == ["SUCCEEDED"]
    assert [
        transition["to_state"] for transition in timeline["transitions"]
    ] == ["CREATED", "PROCESSING", "UNKNOWN", "SUCCEEDED"]
    assert timeline["provider_events"][0]["provider_event_id"] == callback[
        "provider_event_id"
    ]
    assert timeline["provider_events"][0]["processing_result"] == "APPLIED_SUCCEEDED"
    assert timeline["reconciliation_jobs"][0]["status"] == "PENDING"
