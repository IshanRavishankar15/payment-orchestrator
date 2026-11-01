"""Constrain durable job providers and lease state.

Revision ID: 0005_job_claim_constraints
Revises: 0004_durable_worker
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_job_claim_constraints"
down_revision: Union[str, None] = "0004_durable_worker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_durable_jobs_valid_provider_name"),
        "durable_jobs",
        "provider_name IN ('provider_a', 'provider_b')",
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_valid_lease"),
        "durable_jobs",
        "(status = 'PROCESSING' AND lease_owner IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'PROCESSING' AND lease_owner IS NULL "
        "AND lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_durable_jobs_valid_lease"), "durable_jobs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_durable_jobs_valid_provider_name"),
        "durable_jobs",
        type_="check",
    )
