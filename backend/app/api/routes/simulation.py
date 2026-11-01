import hmac
import json

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.schemas.payment import (
    PaymentIntentRead,
    ProviderCallback,
    SimulationPaymentCreate,
)
from app.services.payment_intents import (
    IdempotencyKeyConflict,
    create_payment_intent,
)
from app.services.provider_callbacks import (
    CallbackReferenceNotFound,
    process_provider_callback,
)
from app.db.session import SessionLocal

router = APIRouter(prefix="/simulation", tags=["simulation"])


def _require_admin_token(supplied_token: str | None) -> None:
    configured_token = get_settings().simulation_admin_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "simulation_admin_not_configured",
                "message": "Simulation controls are disabled until an admin token is configured.",
            },
        )
    if supplied_token is None or not hmac.compare_digest(
        configured_token, supplied_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_simulation_admin_token",
                "message": "A valid simulation admin token is required.",
            },
        )


@router.post(
    "/payments",
    response_model=PaymentIntentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_simulated_payment(
    payload: SimulationPaymentCreate,
    response: Response,
    admin_token: str | None = Header(
        default=None, alias="X-Simulation-Admin-Token"
    ),
    session: Session = Depends(get_db),
) -> PaymentIntentRead:
    _require_admin_token(admin_token)
    try:
        payment, merchant_id, created = create_payment_intent(
            session,
            payload,
            scenario=payload.scenario,
            provider_name=payload.provider_name,
        )
    except IdempotencyKeyConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_payload_mismatch",
                "message": "This merchant has already used the idempotency key with a different payload.",
            },
        ) from error

    if not created:
        response.status_code = status.HTTP_200_OK
    return PaymentIntentRead(
        id=payment.id,
        merchant_id=merchant_id,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        state=payment.current_state,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


@router.post("/providers/{provider_name}/callbacks")
async def receive_provider_callback(
    provider_name: str,
    request: Request,
    signature: str | None = Header(default=None, alias="X-Provider-Signature"),
) -> dict[str, str]:
    settings = get_settings()
    secret = {
        "provider_a": settings.provider_a_callback_secret,
        "provider_b": settings.provider_b_callback_secret,
    }.get(provider_name)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "provider_callback_auth_not_configured",
                "message": "Provider callback authentication is not configured.",
            },
        )
    body = await request.body()
    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, digestmod="sha256"
    ).hexdigest()
    if signature is None or not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_provider_signature",
                "message": "The provider callback signature is invalid.",
            },
        )

    try:
        callback = ProviderCallback.model_validate_json(body)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=json.loads(error.json()),
        ) from error

    try:
        result = process_provider_callback(
            SessionLocal,
            provider_name,
            callback.model_dump(),
        )
    except CallbackReferenceNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "provider_reference_not_found",
                "message": "No attempt exists for this provider reference.",
            },
        ) from error
    return {"status": result.lower()}
