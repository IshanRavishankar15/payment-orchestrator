import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DurableJob(Base):
    __tablename__ = "durable_jobs"
    __table_args__ = (
        Index("ix_durable_jobs_status_available_at", "status", "available_at"),
        UniqueConstraint("dedupe_key", name="uq_durable_jobs_dedupe_key"),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'DEAD')",
            name="valid_status",
        ),
        CheckConstraint(
            "job_type IN ('PROCESS_PAYMENT', 'RECONCILE_PAYMENT')",
            name="valid_job_type",
        ),
        CheckConstraint(
            "scenario IN ('SUCCESS', 'DECLINE', 'TIMEOUT', "
            "'SUCCESS_LOST_RESPONSE', 'TEMPORARY_UNAVAILABLE')",
            name="valid_scenario",
        ),
        CheckConstraint(
            "provider_name IN ('provider_a', 'provider_b')",
            name="valid_provider_name",
        ),
        CheckConstraint(
            "(status = 'PROCESSING' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'PROCESSING' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="valid_lease",
        ),
        CheckConstraint("retry_count >= 0", name="nonnegative_retry_count"),
        CheckConstraint("max_retries >= 0", name="nonnegative_max_retries"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payment_intents.id"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payment_attempts.id")
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False
    )
    provider_name: Mapped[str] = mapped_column(
        String(80), default="provider_a", server_default="provider_a", nullable=False
    )
    scenario: Mapped[str] = mapped_column(
        String(32), default="SUCCESS", server_default="SUCCESS", nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
