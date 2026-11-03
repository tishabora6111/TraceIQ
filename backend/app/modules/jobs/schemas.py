import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})
ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


class JobOutcome(StrEnum):
    """Result of executing one claimed job."""

    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    # The worker no longer owned the job (lease expired, recovered, or reclaimed); nothing was recorded.
    LOST_OWNERSHIP = "lost_ownership"


class RecoveredJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    status: JobStatus
    previous_worker: str | None


class ScheduleSyncResult(BaseModel):
    """What startup synchronization changed in ``job_schedules``."""

    model_config = ConfigDict(frozen=True)

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()


class ScheduleRunOutcome(StrEnum):
    ENQUEUED = "enqueued"
    # The previous run (dedupe key "schedule:<name>") is still queued or running; this run is skipped.
    SKIPPED_ACTIVE = "skipped_active"
    # This worker cannot build the job (type not registered here, or payload no longer valid);
    # the schedule is left unadvanced for a worker that can.
    SKIPPED_UNKNOWN_TYPE = "skipped_unknown_type"
    SKIPPED_INVALID_PAYLOAD = "skipped_invalid_payload"


class ScheduleRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    outcome: ScheduleRunOutcome
    job_id: uuid.UUID | None = None
    next_run_at: datetime | None = None


class JobRead(BaseModel):
    """Read-only snapshot of a job row."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    type: str
    status: JobStatus
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    run_at: datetime
    locked_by: str | None
    locked_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    updated_at: datetime
