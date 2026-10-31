import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class ProviderSimulationTransaction(Base):
    __tablename__ = "provider_simulation_transactions"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "provider_reference",
            name="uq_provider_simulation_transactions_provider_reference",
        ),
        CheckConstraint(
            "scenario IN ('SUCCESS', 'DECLINE', 'TIMEOUT', "
            "'SUCCESS_LOST_RESPONSE', 'TEMPORARY_UNAVAILABLE')",
            name="valid_scenario",
        ),
        CheckConstraint(
            "outcome IN ('SUCCEEDED', 'DECLINED', 'UNKNOWN', 'NOT_ACCEPTED')",
            name="valid_outcome",
        ),
        CheckConstraint("amount_minor > 0", name="amount_minor_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payment_intents.id"), nullable=False
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    scenario: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    status_query_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
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
