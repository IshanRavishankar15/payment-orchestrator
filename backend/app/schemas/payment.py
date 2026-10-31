import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


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
