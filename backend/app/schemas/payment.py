import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.providers.base import ProviderScenario


MerchantIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
IdempotencyKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
CurrencyCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z]{3}$", min_length=3, max_length=3)
]


class PaymentState(StrEnum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PaymentIntentCreate(BaseModel):
    merchant_id: MerchantIdentifier
    amount_minor: Annotated[int, Field(gt=0, le=2_147_483_647)]
    currency: CurrencyCode
    idempotency_key: IdempotencyKey


class SimulationPaymentCreate(PaymentIntentCreate):
    scenario: ProviderScenario = ProviderScenario.SUCCESS
    provider_name: Annotated[
        str, StringConstraints(pattern=r"^(provider_a|provider_b)$")
    ] = "provider_a"


class ProviderCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_event_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    event_type: Annotated[
        str, StringConstraints(pattern=r"^(PAYMENT_SUCCEEDED|PAYMENT_DECLINED)$")
    ]
    provider_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]


class PaymentIntentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: str
    amount_minor: int
    currency: str
    state: PaymentState
    created_at: datetime
    updated_at: datetime


class PaymentIntentList(BaseModel):
    items: list[PaymentIntentRead]
    limit: int
    offset: int


class PaymentAttemptRead(BaseModel):
    id: uuid.UUID
    provider_name: str
    attempt_number: int
    provider_reference: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    reason: str | None


class StateTransitionRead(BaseModel):
    id: uuid.UUID
    from_state: PaymentState | None
    to_state: PaymentState
    reason: str
    attempt_id: uuid.UUID | None
    provider_event_row_id: uuid.UUID | None
    occurred_at: datetime


class ProviderEventRead(BaseModel):
    id: uuid.UUID
    provider_name: str
    provider_event_id: str
    event_type: str
    signature_valid: bool
    received_at: datetime
    processed_at: datetime | None
    processing_result: str | None


class ReconciliationJobRead(BaseModel):
    id: uuid.UUID
    attempt_id: uuid.UUID | None
    status: str
    retry_count: int
    max_retries: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class PaymentTimelineRead(BaseModel):
    payment: PaymentIntentRead
    attempts: list[PaymentAttemptRead]
    transitions: list[StateTransitionRead]
    provider_events: list[ProviderEventRead]
    reconciliation_jobs: list[ReconciliationJobRead]
