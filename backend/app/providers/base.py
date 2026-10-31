from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
import uuid


class ProviderScenario(StrEnum):
    SUCCESS = "SUCCESS"
    DECLINE = "DECLINE"
    TIMEOUT = "TIMEOUT"
    SUCCESS_LOST_RESPONSE = "SUCCESS_LOST_RESPONSE"
    TEMPORARY_UNAVAILABLE = "TEMPORARY_UNAVAILABLE"


class ProviderOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"
    NOT_ACCEPTED = "NOT_ACCEPTED"


@dataclass(frozen=True)
class ProviderResult:
    provider_reference: str
    outcome: ProviderOutcome
    accepted: bool | None


class ProviderError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class ProviderTimeout(ProviderError):
    def __init__(self) -> None:
        super().__init__(
            "provider_timeout",
            "The provider response was not received before the timeout.",
        )


class ProviderUnavailable(ProviderError):
    def __init__(self) -> None:
        super().__init__(
            "provider_temporarily_unavailable",
            "The provider was unavailable before accepting the request.",
        )


class ProviderReferenceConflict(ProviderError):
    def __init__(self) -> None:
        super().__init__(
            "provider_reference_conflict",
            "The provider reference is already associated with a different request.",
        )


class ProviderAdapter(Protocol):
    name: str

    def submit(
        self,
        payment_id: uuid.UUID,
        amount_minor: int,
        currency: str,
        provider_reference: str,
        scenario: ProviderScenario,
    ) -> ProviderResult: ...

    def get_status(self, provider_reference: str) -> ProviderResult | None: ...
