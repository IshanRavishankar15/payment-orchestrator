"""Persist simulated provider outcomes and initialize transition history.

Revision ID: 0003_provider_simulation
Revises: 0002_job_dedupe
"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_provider_simulation"
down_revision: Union[str, None] = "0002_job_dedupe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_simulation_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_reference", sa.String(length=255), nullable=False),
        sa.Column("scenario", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column(
            "status_query_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scenario IN ('SUCCESS', 'DECLINE', 'TIMEOUT', "
            "'SUCCESS_LOST_RESPONSE', 'TEMPORARY_UNAVAILABLE')",
            name="ck_provider_simulation_transactions_valid_scenario",
        ),
        sa.CheckConstraint(
            "outcome IN ('SUCCEEDED', 'DECLINED', 'UNKNOWN', 'NOT_ACCEPTED')",
            name="ck_provider_simulation_transactions_valid_outcome",
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name="ck_provider_simulation_transactions_amount_minor_positive",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment_intents.id"],
            name="fk_provider_simulation_transactions_payment_id_payment_intents",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name="fk_provider_simulation_transactions_provider_id_providers",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_provider_simulation_transactions"
        ),
        sa.UniqueConstraint(
            "provider_id",
            "provider_reference",
            name="uq_provider_simulation_transactions_provider_reference",
        ),
    )

    connection = op.get_bind()
    payments = sa.table(
        "payment_intents",
        sa.column("id", sa.Uuid()),
        sa.column("current_state", sa.String(16)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    transitions = sa.table(
        "state_transitions",
        sa.column("id", sa.Uuid()),
        sa.column("payment_id", sa.Uuid()),
        sa.column("from_state", sa.String(16)),
        sa.column("to_state", sa.String(16)),
        sa.column("reason", sa.String(255)),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
    )
    existing_transition = (
        sa.select(transitions.c.id)
        .where(transitions.c.payment_id == payments.c.id)
        .exists()
    )
    rows = connection.execute(
        sa.select(payments.c.id, payments.c.current_state, payments.c.created_at).where(
            ~existing_transition
        )
    )
    for payment_id, current_state, created_at in rows:
        connection.execute(
            transitions.insert().values(
                id=uuid.uuid4(),
                payment_id=payment_id,
                from_state=None,
                to_state=current_state,
                reason="State history initialized by migration",
                occurred_at=created_at,
            )
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM state_transitions "
        "WHERE reason = 'State history initialized by migration'"
    )
    op.drop_table("provider_simulation_transactions")
