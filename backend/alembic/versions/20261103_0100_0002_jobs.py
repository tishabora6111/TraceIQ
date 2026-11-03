"""Jobs: background job queue and interval schedules.

Revision ID: 0002_jobs
Revises: 0001_baseline
Create Date: 2026-11-03 01:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_jobs"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name=op.f("ck_jobs_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
        sa.CheckConstraint("attempts <= max_attempts", name=op.f("ck_jobs_attempts_within_max")),
        sa.CheckConstraint(
            "status <> 'running' OR (locked_by IS NOT NULL AND locked_at IS NOT NULL"
            " AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)",
            name=op.f("ck_jobs_running_is_locked"),
        ),
        sa.CheckConstraint(
            "status = 'running' OR (locked_by IS NULL AND locked_at IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_jobs_not_running_is_unlocked"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_jobs_finished_iff_terminal"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_queued_run_at",
        "jobs",
        ["run_at", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_running_lease_expires_at",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_jobs_type_status", "jobs", ["type", "status"])
    op.create_index("ix_jobs_finished_at", "jobs", ["finished_at"])
    op.create_index(
        "uq_jobs_active_dedupe_key",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
    )

    op.create_table(
        "job_schedules",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("job_type", sa.String(length=255), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("interval_seconds > 0", name=op.f("ck_job_schedules_interval_positive")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_job_schedules")),
    )
    op.create_index(
        "ix_job_schedules_enabled_next_run_at",
        "job_schedules",
        ["next_run_at"],
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_schedules_enabled_next_run_at", table_name="job_schedules")
    op.drop_table("job_schedules")
    op.drop_index("uq_jobs_active_dedupe_key", table_name="jobs")
    op.drop_index("ix_jobs_finished_at", table_name="jobs")
    op.drop_index("ix_jobs_type_status", table_name="jobs")
    op.drop_index("ix_jobs_running_lease_expires_at", table_name="jobs")
    op.drop_index("ix_jobs_queued_run_at", table_name="jobs")
    op.drop_table("jobs")
