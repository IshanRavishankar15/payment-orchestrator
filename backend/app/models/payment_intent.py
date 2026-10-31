import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "idempotency_key",
            name="uq_payment_intents_merchant_id_idempotency_key",
        ),
        CheckConstraint("amount_minor > 0", name="amount_minor_positive"),
        CheckConstraint(
            "current_state IN ('CREATED', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="valid_current_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("merchants.id"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    current_state: Mapped[str] = mapped_column(
        String(16), default="CREATED", server_default="CREATED", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
