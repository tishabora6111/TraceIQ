"""Public interface of the jobs module."""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.modules.jobs import repository
from app.modules.jobs.errors import DuplicateJobError
from app.modules.jobs.registry import (
    JobPayload,
    JobRegistry,
    default_registry,
    serialize_payload,
    validate_payload,
)
from app.modules.jobs.schemas import JobRead

MAX_DEDUPE_KEY_LENGTH = 255


def enqueue(
    session: Session,
    job_type: str,
    payload: JobPayload | Mapping[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    registry: JobRegistry = default_registry,
    settings: Settings | None = None,
) -> JobRead:
    """Add a queued job to the caller's transaction and return it.

    The job becomes visible to workers only when the caller commits. This function never
    commits or rolls back. ``run_at`` defaults to the database's ``now()``.

    Raises ``UnknownJobTypeError``, ``InvalidJobPayloadError``, ``ValueError`` (bad ``run_at`` or
    ``dedupe_key``), or ``DuplicateJobError`` if an active job already has ``dedupe_key``; in that
    case nothing is inserted and the caller's transaction remains usable.
    """
    handler = registry.get(job_type)
    validated = validate_payload(handler, payload)
    data = serialize_payload(handler, validated)

    if run_at is not None and (run_at.tzinfo is None or run_at.utcoffset() is None):
        raise ValueError("run_at must be timezone-aware")
    if dedupe_key is not None and not (0 < len(dedupe_key) <= MAX_DEDUPE_KEY_LENGTH):
        raise ValueError(f"dedupe_key must be 1-{MAX_DEDUPE_KEY_LENGTH} characters")

    settings = settings or get_settings()
    row = repository.insert_job(
        session,
        job_id=uuid.uuid4(),
        job_type=handler.name,
        payload=data,
        max_attempts=handler.max_attempts or settings.job_default_max_attempts,
        run_at=run_at,
        dedupe_key=dedupe_key,
    )
    if row is None:
        assert dedupe_key is not None
        raise DuplicateJobError(dedupe_key, repository.find_active_job_id_by_dedupe_key(session, dedupe_key))
    return JobRead.model_validate(row)


def get_job(session: Session, job_id: uuid.UUID) -> JobRead | None:
    """Return the job, or ``None`` if no job has this id."""
    row = repository.get_job_row(session, job_id)
    return JobRead.model_validate(row) if row is not None else None
