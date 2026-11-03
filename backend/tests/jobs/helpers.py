"""Shared helpers for job tests. Test handlers live in per-test registries, never the production one."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from app.core.database import get_session_factory
from app.modules.jobs.registry import JobPayload, JobRegistry
from app.modules.jobs.service import enqueue


class EmptyPayload(JobPayload):
    pass


class MarkerPayload(JobPayload):
    marker: str


def create_markers_table() -> None:
    """A scratch table handlers write to, to observe whether their writes were committed."""
    with get_session_factory()() as session, session.begin():
        session.execute(text("DROP TABLE IF EXISTS test_job_markers"))
        session.execute(text("CREATE TABLE test_job_markers (marker text NOT NULL)"))


def drop_markers_table() -> None:
    # Dropped after each test so `alembic check` never sees a table outside the models.
    with get_session_factory()() as session, session.begin():
        session.execute(text("DROP TABLE IF EXISTS test_job_markers"))


def markers() -> list[str]:
    with get_session_factory()() as session:
        return list(session.scalars(text("SELECT marker FROM test_job_markers ORDER BY marker")))


# The database host clock can step backwards slightly (observed ~0.5 s on Docker Desktop), so a
# job enqueued with run_at = now() may briefly look "not yet due" to the next transaction. Tests
# that need a due job therefore use clear margins rather than relying on "just now".
CLOCK_MARGIN = timedelta(minutes=1)


def enqueue_committed(
    registry: JobRegistry, job_type: str, payload: dict[str, Any] | None = None, **options: Any
) -> uuid.UUID:
    """Enqueue and commit a job. Unless ``run_at`` is given, the job is already due (by a margin)."""
    options.setdefault("run_at", db_now() - CLOCK_MARGIN)
    with get_session_factory()() as session, session.begin():
        return enqueue(session, job_type, payload or {}, registry=registry, **options).id


def job_row(job_id: uuid.UUID) -> Any:
    with get_session_factory()() as session:
        return session.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


def db_now() -> Any:
    with get_session_factory()() as session:
        return session.scalar(text("SELECT clock_timestamp()"))


def expire_lease(job_id: uuid.UUID) -> None:
    """Move a running job's lease into the past (as if its worker stalled)."""
    with get_session_factory()() as session, session.begin():
        session.execute(
            text(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 minute'"
                " WHERE id = :id AND status = 'running'"
            ),
            {"id": job_id},
        )


def make_due(job_id: uuid.UUID) -> None:
    """Make a queued job (e.g. one waiting for a retry backoff) due now."""
    with get_session_factory()() as session, session.begin():
        session.execute(
            text("UPDATE jobs SET run_at = now() - interval '1 minute' WHERE id = :id AND status = 'queued'"),
            {"id": job_id},
        )
