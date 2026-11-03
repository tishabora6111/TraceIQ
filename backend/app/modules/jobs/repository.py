"""SQL for the jobs module. Functions run in the caller's session and never commit.

Time comparisons that must reflect the real current time use ``clock_timestamp()``: ``now()``
is fixed at the start of the transaction, which for a handler transaction may be long ago.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Row, bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session

from app.modules.jobs.models import Job
from app.modules.jobs.schemas import ACTIVE_STATUSES, JobStatus

_JOBS = Job.__table__
# Must match the predicate of the uq_jobs_active_dedupe_key partial unique index.
_ACTIVE_DEDUPE_PREDICATE = text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')")

# Ownership of a running job: same claim (worker + attempt) and an unexpired lease.
_OWNED = (
    "id = :job_id AND status = 'running' AND locked_by = :worker_id AND attempts = :attempt"
    " AND lease_expires_at > clock_timestamp()"
)


def insert_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any],
    max_attempts: int,
    run_at: datetime | None,
    dedupe_key: str | None,
) -> Row[Any] | None:
    """Insert a queued job and return its row, or ``None`` if an active job has the same dedupe key.

    The conflict is resolved with ``ON CONFLICT DO NOTHING`` rather than a unique violation, so the
    caller's transaction stays usable. If another uncommitted transaction holds the same key,
    PostgreSQL waits for it to finish before deciding.
    """
    values: dict[str, Any] = {
        "id": job_id,
        "type": job_type,
        "status": JobStatus.QUEUED.value,
        "payload": payload,
        "attempts": 0,
        "max_attempts": max_attempts,
        "dedupe_key": dedupe_key,
    }
    if run_at is not None:
        values["run_at"] = run_at

    statement = insert(_JOBS).values(**values)
    if dedupe_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[_JOBS.c.dedupe_key], index_where=_ACTIVE_DEDUPE_PREDICATE
        )
    return session.execute(statement.returning(*_JOBS.c)).first()


def find_active_job_id_by_dedupe_key(session: Session, dedupe_key: str) -> uuid.UUID | None:
    return session.scalar(
        select(_JOBS.c.id).where(
            _JOBS.c.dedupe_key == dedupe_key,
            _JOBS.c.status.in_([status.value for status in ACTIVE_STATUSES]),
        )
    )


def get_job_row(session: Session, job_id: uuid.UUID) -> Row[Any] | None:
    return session.execute(select(*_JOBS.c).where(_JOBS.c.id == job_id)).first()


def claim_next_job(session: Session, *, worker_id: str, lease_seconds: int) -> Row[Any] | None:
    """Lock the oldest due queued job, mark it running for ``worker_id``, and return it.

    ``SKIP LOCKED`` lets concurrent workers claim different jobs without blocking each other.
    """
    return session.execute(
        text(
            """
            WITH next_job AS (
                SELECT id FROM jobs
                WHERE status = 'queued' AND run_at <= now()
                ORDER BY run_at, created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs SET
                status = 'running',
                attempts = jobs.attempts + 1,
                locked_by = :worker_id,
                locked_at = now(),
                started_at = now(),
                lease_expires_at = now() + make_interval(secs => :lease_seconds),
                updated_at = now()
            FROM next_job
            WHERE jobs.id = next_job.id
            RETURNING jobs.*
            """
        ),
        {"worker_id": worker_id, "lease_seconds": lease_seconds},
    ).first()


def set_lease(session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> Row[Any] | None:
    """Set the lease of a job just claimed in this transaction, measured from its lock time."""
    return session.execute(
        text(
            "UPDATE jobs SET lease_expires_at = locked_at + make_interval(secs => :lease_seconds)"
            " WHERE id = :job_id AND status = 'running' AND locked_by = :worker_id RETURNING *"
        ),
        {"job_id": job_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
    ).first()


def mark_succeeded(session: Session, *, job_id: uuid.UUID, worker_id: str, attempt: int) -> bool:
    """Mark an owned job succeeded. Returns False (and changes nothing) if ownership was lost.

    Runs inside the handler's transaction; the row lock it takes serializes it against recovery
    and re-claiming, so the ownership check and the commit cannot interleave with them.
    """
    result = session.execute(
        text(
            f"""
            UPDATE jobs SET
                status = 'succeeded',
                finished_at = clock_timestamp(),
                last_error = NULL,
                locked_by = NULL,
                locked_at = NULL,
                lease_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE {_OWNED}
            RETURNING id
            """
        ),
        {"job_id": job_id, "worker_id": worker_id, "attempt": attempt},
    ).first()
    return result is not None


def record_failure(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    attempt: int,
    error: str,
    retry_delay_seconds: float | None,
) -> Row[Any] | None:
    """Record a failed attempt of an owned job.

    With ``retry_delay_seconds`` the job returns to ``queued`` and becomes due after the delay;
    with ``None`` it becomes ``failed``. Returns the updated row, or ``None`` if ownership was lost.
    """
    if retry_delay_seconds is None:
        assignments = "status = 'failed', finished_at = clock_timestamp()"
    else:
        assignments = "status = 'queued', run_at = clock_timestamp() + make_interval(secs => :delay)"
    return session.execute(
        text(
            f"""
            UPDATE jobs SET
                {assignments},
                last_error = :error,
                locked_by = NULL,
                locked_at = NULL,
                lease_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE {_OWNED}
            RETURNING *
            """
        ),
        {"job_id": job_id, "worker_id": worker_id, "attempt": attempt, "error": error, "delay": retry_delay_seconds},
    ).first()


def recover_stale_jobs(session: Session, *, limit: int) -> list[Row[Any]]:
    """Release running jobs whose lease has expired.

    Jobs with attempts remaining return to ``queued`` (due immediately); exhausted jobs become
    ``failed``. Rows locked by another transaction (e.g. a worker completing its job right now,
    or another recovery run) are skipped.
    """
    return list(
        session.execute(
            text(
                """
                WITH stale AS (
                    SELECT id, locked_by FROM jobs
                    WHERE status = 'running' AND lease_expires_at < clock_timestamp()
                    ORDER BY lease_expires_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                )
                UPDATE jobs SET
                    status = CASE WHEN jobs.attempts >= jobs.max_attempts THEN 'failed' ELSE 'queued' END,
                    finished_at = CASE WHEN jobs.attempts >= jobs.max_attempts THEN clock_timestamp() END,
                    run_at = CASE WHEN jobs.attempts >= jobs.max_attempts THEN jobs.run_at ELSE clock_timestamp() END,
                    last_error = 'Lease expired before the job finished',
                    locked_by = NULL,
                    locked_at = NULL,
                    lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                FROM stale
                WHERE jobs.id = stale.id
                RETURNING jobs.id, jobs.status, stale.locked_by AS previous_worker
                """
            ),
            {"limit": limit},
        ).all()
    )


# --- Schedules -------------------------------------------------------------------------------

# Any fixed key works; it only has to be unique among advisory locks used by the application.
_SCHEDULE_SYNC_LOCK_KEY = "traceiq.jobs.schedule_sync"


def lock_schedule_sync(session: Session) -> None:
    """Serialize schedule synchronization across workers for the rest of this transaction."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _SCHEDULE_SYNC_LOCK_KEY})


def upsert_schedule(
    session: Session, *, name: str, job_type: str, interval_seconds: int, payload: dict[str, Any]
) -> str | None:
    """Create or update a schedule from its code definition.

    Returns ``"created"``, ``"updated"``, or ``None`` if the stored definition already matches.
    New schedules are due immediately. An update keeps ``next_run_at`` unless the interval
    changed, in which case it may only move earlier: to one new interval after the last run
    (or after now, if it never ran). Re-enabling a disabled schedule keeps its ``next_run_at``,
    so an overdue one runs once on the next tick.
    """
    row = session.execute(
        text(
            """
            INSERT INTO job_schedules
                (name, job_type, interval_seconds, payload, enabled, next_run_at, created_at, updated_at)
            VALUES (:name, :job_type, :interval_seconds, :payload, true, now(), now(), now())
            ON CONFLICT (name) DO UPDATE SET
                job_type = EXCLUDED.job_type,
                interval_seconds = EXCLUDED.interval_seconds,
                payload = EXCLUDED.payload,
                enabled = true,
                next_run_at = CASE
                    WHEN job_schedules.interval_seconds <> EXCLUDED.interval_seconds THEN LEAST(
                        job_schedules.next_run_at,
                        COALESCE(job_schedules.last_enqueued_at, now())
                            + make_interval(secs => EXCLUDED.interval_seconds)
                    )
                    ELSE job_schedules.next_run_at
                END,
                updated_at = now()
            WHERE (job_schedules.job_type, job_schedules.interval_seconds, job_schedules.payload, job_schedules.enabled)
                IS DISTINCT FROM (EXCLUDED.job_type, EXCLUDED.interval_seconds, EXCLUDED.payload, true)
            RETURNING (xmax = 0) AS inserted
            """
        ).bindparams(bindparam("payload", type_=JSONB)),
        {"name": name, "job_type": job_type, "interval_seconds": interval_seconds, "payload": payload},
    ).first()
    if row is None:
        return None
    return "created" if row.inserted else "updated"


def disable_schedules_except(session: Session, names: list[str]) -> list[str]:
    """Disable enabled schedules not in ``names``. Rows (and their history) are kept."""
    return list(
        session.scalars(
            text(
                "UPDATE job_schedules SET enabled = false, updated_at = now()"
                " WHERE enabled AND name <> ALL(CAST(:names AS text[])) RETURNING name"
            ),
            {"names": names},
        )
    )


def lock_due_schedules(session: Session, *, limit: int) -> list[Row[Any]]:
    """Lock enabled, due schedules and compute each one's next run time.

    ``new_next_run_at`` is the first point strictly after ``now()`` on the schedule's grid
    (``next_run_at + k * interval``), so however long the downtime, a schedule runs once and
    keeps its phase. Rows locked by a concurrent tick are skipped; a row another tick has
    already advanced is re-checked by PostgreSQL and no longer matches.
    """
    return list(
        session.execute(
            text(
                """
                SELECT
                    name, job_type, interval_seconds, payload, next_run_at,
                    next_run_at + make_interval(secs => interval_seconds * (
                        floor(extract(epoch FROM (now() - next_run_at)) / interval_seconds) + 1
                    )) AS new_next_run_at
                FROM job_schedules
                WHERE enabled AND next_run_at <= now()
                ORDER BY next_run_at, name
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).all()
    )


def advance_schedule(session: Session, *, name: str, next_run_at: datetime, enqueued: bool) -> None:
    session.execute(
        text(
            "UPDATE job_schedules SET next_run_at = :next_run_at,"
            " last_enqueued_at = CASE WHEN :enqueued THEN now() ELSE last_enqueued_at END,"
            " updated_at = now() WHERE name = :name"
        ),
        {"name": name, "next_run_at": next_run_at, "enqueued": enqueued},
    )
