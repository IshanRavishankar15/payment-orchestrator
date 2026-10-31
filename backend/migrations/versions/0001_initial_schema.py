"""Create the initial payment orchestration schema.

Revision ID: 0001_initial
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_merchants"),
        sa.UniqueConstraint("external_id", name="uq_merchants_external_id"),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_providers"),
        sa.UniqueConstraint("name", name="uq_providers_name"),
    )
    op.create_table(
        "payment_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("current_state", sa.String(length=16), server_default="CREATED", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_payment_intents_amount_minor_positive"),
        sa.CheckConstraint(
            "current_state IN ('CREATED', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="ck_payment_intents_valid_current_state",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_payment_intents_merchant_id_merchants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_intents"),
        sa.UniqueConstraint(
            "merchant_id",
            "idempotency_key",
            name="uq_payment_intents_merchant_id_idempotency_key",
        ),
    )
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment_intents.id"],
            name="fk_payment_attempts_payment_id_payment_intents",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name="fk_payment_attempts_provider_id_providers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_attempts"),
        sa.UniqueConstraint(
            "payment_id", "attempt_number", name="uq_payment_attempts_payment_attempt_number"
        ),
    )
    op.create_table(
        "provider_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_result", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name="fk_provider_events_provider_id_providers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_events"),
        sa.UniqueConstraint(
            "provider_id", "provider_event_id", name="uq_provider_events_provider_event_identity"
        ),
    )
    op.create_table(
        "durable_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=40), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["payment_attempts.id"],
            name="fk_durable_jobs_attempt_id_payment_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment_intents.id"],
            name="fk_durable_jobs_payment_id_payment_intents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_durable_jobs"),
    )
    op.create_index(
        "ix_durable_jobs_status_available_at",
        "durable_jobs",
        ["status", "available_at"],
        unique=False,
    )
    op.create_table(
        "state_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("provider_event_row_id", sa.Uuid(), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["payment_attempts.id"],
            name="fk_state_transitions_attempt_id_payment_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment_intents.id"],
            name="fk_state_transitions_payment_id_payment_intents",
        ),
        sa.ForeignKeyConstraint(
            ["provider_event_row_id"],
            ["provider_events.id"],
            name="fk_state_transitions_provider_event_row_id_provider_events",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_state_transitions"),
    )
    op.create_index(
        "ix_state_transitions_payment_id_occurred_at",
        "state_transitions",
        ["payment_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_state_transitions_payment_id_occurred_at", table_name="state_transitions")
    op.drop_table("state_transitions")
    op.drop_index("ix_durable_jobs_status_available_at", table_name="durable_jobs")
    op.drop_table("durable_jobs")
    op.drop_table("provider_events")
    op.drop_table("payment_attempts")
    op.drop_table("payment_intents")
    op.drop_table("providers")
    op.drop_table("merchants")
