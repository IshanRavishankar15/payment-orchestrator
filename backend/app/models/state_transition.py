import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StateTransition(Base):
    __tablename__ = "state_transitions"
    __table_args__ = (
        Index("ix_state_transitions_payment_id_occurred_at", "payment_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payment_intents.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payment_attempts.id")
    )
    provider_event_row_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_events.id")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
