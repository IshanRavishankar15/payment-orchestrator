import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.main import app
from app.models.durable_job import DurableJob
from app.models.payment_intent import PaymentIntent
from app.models.state_transition import StateTransition


def _new_payload() -> dict[str, object]:
    return {
        "merchant_id": f"test-merchant-{uuid.uuid4()}",
        "amount_minor": 1250,
        "currency": "INR",
        "idempotency_key": f"request-{uuid.uuid4()}",
    }


def _create_payment(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    with TestClient(app) as client:
        response = client.post("/payments", json=payload)
    return response.status_code, response.json()


def test_create_payment_is_idempotent_and_schedules_one_job() -> None:
    payload = _new_payload()

    first_status, first_body = _create_payment(payload)
    duplicate_status, duplicate_body = _create_payment(payload)

    assert first_status == 201
    assert duplicate_status == 200
    assert duplicate_body["id"] == first_body["id"]
    assert first_body["state"] == "CREATED"

    with SessionLocal() as session:
        job_count = session.scalar(
            select(func.count())
            .select_from(DurableJob)
            .where(DurableJob.payment_id == uuid.UUID(first_body["id"]))
        )
        transitions = session.scalars(
            select(StateTransition).where(
                StateTransition.payment_id == uuid.UUID(first_body["id"])
            )
        ).all()
    assert job_count == 1
    assert len(transitions) == 1
    assert transitions[0].from_state is None
    assert transitions[0].to_state == "CREATED"


def test_reusing_idempotency_key_with_different_payload_is_conflict() -> None:
    payload = _new_payload()
    status_code, first_body = _create_payment(payload)
    assert status_code == 201

    mismatched_payload = {**payload, "amount_minor": 1300}
    mismatch_status, mismatch_body = _create_payment(mismatched_payload)

    assert mismatch_status == 409
    assert mismatch_body["detail"]["code"] == "idempotency_key_payload_mismatch"
    with SessionLocal() as session:
        payment_count = session.scalar(
            select(func.count())
            .select_from(PaymentIntent)
            .where(PaymentIntent.id == uuid.UUID(first_body["id"]))
        )
        job_count = session.scalar(
            select(func.count())
            .select_from(DurableJob)
            .where(DurableJob.payment_id == uuid.UUID(first_body["id"]))
        )
    assert payment_count == 1
    assert job_count == 1


def test_idempotency_keys_are_scoped_to_merchant() -> None:
    idempotency_key = f"shared-key-{uuid.uuid4()}"
    first_payload = {
        **_new_payload(),
        "idempotency_key": idempotency_key,
    }
    second_payload = {
        **_new_payload(),
        "idempotency_key": idempotency_key,
    }

    first_status, first_body = _create_payment(first_payload)
    second_status, second_body = _create_payment(second_payload)

    assert first_status == 201
    assert second_status == 201
    assert first_body["id"] != second_body["id"]


def test_concurrent_duplicate_requests_create_one_payment_and_job() -> None:
    payload = _new_payload()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(_create_payment, [payload] * 8))

    assert all(status in (200, 201) for status, _ in results)
    payment_ids = {body["id"] for _, body in results}
    assert len(payment_ids) == 1
    payment_id = uuid.UUID(payment_ids.pop())

    with SessionLocal() as session:
        payment_count = session.scalar(
            select(func.count())
            .select_from(PaymentIntent)
            .where(PaymentIntent.id == payment_id)
        )
        job_count = session.scalar(
            select(func.count())
            .select_from(DurableJob)
            .where(DurableJob.payment_id == payment_id)
        )
    assert payment_count == 1
    assert job_count == 1


def test_get_and_list_payment_intents() -> None:
    payload = _new_payload()
    status_code, created = _create_payment(payload)
    assert status_code == 201

    with TestClient(app) as client:
        retrieved = client.get(f"/payments/{created['id']}")
        listing = client.get(
            "/payments",
            params={
                "merchant_id": payload["merchant_id"],
                "state": "CREATED",
                "limit": 10,
                "offset": 0,
            },
        )
        missing = client.get(f"/payments/{uuid.uuid4()}")

    assert retrieved.status_code == 200
    assert retrieved.json()["id"] == created["id"]
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [created["id"]]
    assert missing.status_code == 404


def test_create_payment_validates_request_fields() -> None:
    payload = {**_new_payload(), "amount_minor": 0}
    status_code, body = _create_payment(payload)

    assert status_code == 422
    assert body["detail"][0]["loc"][-1] == "amount_minor"
