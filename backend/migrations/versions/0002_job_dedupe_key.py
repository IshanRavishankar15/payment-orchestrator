"""Add a dedupe key to durable jobs.

Revision ID: 0002_job_dedupe
Revises: 0001_initial
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_job_dedupe"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "durable_jobs",
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
    )
    op.execute(
        "UPDATE durable_jobs SET dedupe_key = 'legacy:' || id::text "
        "WHERE dedupe_key IS NULL"
    )
    op.alter_column("durable_jobs", "dedupe_key", nullable=False)
    op.create_unique_constraint(
        "uq_durable_jobs_dedupe_key",
        "durable_jobs",
        ["dedupe_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_durable_jobs_dedupe_key",
        "durable_jobs",
        type_="unique",
    )
    op.drop_column("durable_jobs", "dedupe_key")
