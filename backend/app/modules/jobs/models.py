"""ORM tables owned by the jobs module.

All timestamps are timezone-aware and, where the database sets them, use the database clock.
Primary keys are generated in Python (uuid4); there is no database-side UUID default.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.jobs.schemas import JobStatus

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in JobStatus)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # Total attempts made so far; incremented when a worker claims the job.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Total attempts allowed, including the first.
    max_attempts: Mapped[int] = mapped_column(Integer)
    # Earliest time the job may be claimed.
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_max"),
        CheckConstraint(
            "status <> 'running' OR (locked_by IS NOT NULL AND locked_at IS NOT NULL"
            " AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)",
            name="running_is_locked",
        ),
        CheckConstraint(
            "status = 'running' OR (locked_by IS NULL AND locked_at IS NULL AND lease_expires_at IS NULL)",
            name="not_running_is_unlocked",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name="finished_iff_terminal",
        ),
        # Claiming: oldest due queued job first.
        Index(
            "ix_jobs_queued_run_at",
            "run_at",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        # Stale-job recovery: running jobs by lease expiry.
        Index(
            "ix_jobs_running_lease_expires_at",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_jobs_type_status", "type", "status"),
        Index("ix_jobs_finished_at", "finished_at"),
        # At most one active (queued or running) job per dedupe key.
        Index(
            "uq_jobs_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )


class JobSchedule(Base):
    """An interval-based recurring job definition, synchronized from code by the worker."""

    __tablename__ = "job_schedules"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(255))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # Schedules removed from code are disabled, never deleted.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
        Index("ix_job_schedules_enabled_next_run_at", "next_run_at", postgresql_where=text("enabled")),
    )
