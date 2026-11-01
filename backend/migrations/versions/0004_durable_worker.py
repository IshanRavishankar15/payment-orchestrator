"""Add simulation configuration and durable worker constraints.

Revision ID: 0004_durable_worker
Revises: 0003_provider_simulation
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_durable_worker"
down_revision: Union[str, None] = "0003_provider_simulation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "durable_jobs",
        sa.Column(
            "provider_name",
            sa.String(length=80),
            server_default="provider_a",
            nullable=False,
        ),
    )
    op.add_column(
        "durable_jobs",
        sa.Column(
            "scenario",
            sa.String(length=32),
            server_default="SUCCESS",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_valid_status"),
        "durable_jobs",
        "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'DEAD')",
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_valid_job_type"),
        "durable_jobs",
        "job_type IN ('PROCESS_PAYMENT', 'RECONCILE_PAYMENT')",
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_valid_scenario"),
        "durable_jobs",
        "scenario IN ('SUCCESS', 'DECLINE', 'TIMEOUT', "
        "'SUCCESS_LOST_RESPONSE', 'TEMPORARY_UNAVAILABLE')",
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_nonnegative_retry_count"),
        "durable_jobs",
        "retry_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_durable_jobs_nonnegative_max_retries"),
        "durable_jobs",
        "max_retries >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_durable_jobs_nonnegative_max_retries"),
        "durable_jobs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_durable_jobs_nonnegative_retry_count"),
        "durable_jobs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_durable_jobs_valid_scenario"), "durable_jobs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_durable_jobs_valid_job_type"), "durable_jobs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_durable_jobs_valid_status"), "durable_jobs", type_="check"
    )
    op.drop_column("durable_jobs", "scenario")
    op.drop_column("durable_jobs", "provider_name")
