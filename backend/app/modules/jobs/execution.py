"""Claiming, executing, and recovering jobs.

Each claimed job goes through three separate transactions:

1. **Claim** (short): lock the next due job with ``FOR UPDATE SKIP LOCKED``, mark it running for
   this worker, commit. No transaction is held open while the handler runs.
2. **Execute** (fresh session): run the handler, then mark the job succeeded in the *same*
   transaction, conditional on this worker still owning it (same worker id, same attempt number,
   unexpired lease). If ownership was lost, the whole transaction, including every write the
   handler made, is rolled back.
3. **Record failure** (only on error, fresh transaction): after rolling back the handler's
   writes, schedule a retry with exponential backoff or mark the job failed, under the same
   ownership condition.

Delivery is at-least-once: a handler may run again after a crash or lost lease, so handlers
must be idempotent. Database writes made through ``JobContext.session`` are committed exactly
when the job is marked succeeded.
"""

import logging
import random
import re
from typing import Any

from sqlalchemy import Row, event
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.logging import describe_database_error, find_database_error
from app.modules.jobs import repository
from app.modules.jobs.errors import InvalidJobPayloadError, PermanentJobError, UnknownJobTypeError
from app.modules.jobs.registry import JobContext, JobHandler, JobRegistry, default_registry, validate_payload
from app.modules.jobs.schemas import JobOutcome, JobRead, JobStatus, RecoveredJob

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000
_TRUNCATED = "… [truncated]"
MAX_WORKER_ID_LENGTH = 255
# "scheme://user:password@": the password runs to the last "@" in the URL, so passwords that
# themselves contain "@" are fully masked (over-masking is preferred to leaking).
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]*):\S*@")


class HandlerCommitError(RuntimeError):
    """A handler tried to commit its own transaction, which would bypass the ownership check."""


# --- Helpers ---------------------------------------------------------------------------------


def compute_backoff_seconds(
    attempt: int, *, base_seconds: float, max_seconds: float, rng: random.Random | None = None
) -> float:
    """Delay before retrying after failed attempt number ``attempt`` (1-based).

    ``min(base * 2**(attempt - 1), max)`` scaled by a random factor in [0.5, 1.0] to spread retries.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    exponent = min(attempt - 1, 64)  # avoid float overflow for very large attempt numbers
    ceiling = min(base_seconds * (2.0**exponent), max_seconds)
    return ceiling * (rng or random).uniform(0.5, 1.0)


def format_error(exc: BaseException) -> str:
    """A safe, bounded description of ``exc`` for storage and logs.

    Database errors, and any error caused by one, are reduced to safe metadata (driver error
    class, SQLSTATE, constraint): PostgreSQL messages can contain row values, which
    ``hide_parameters`` does not hide. Otherwise masks passwords in URLs, removes NUL characters
    (not storable in PostgreSQL text), and truncates. Tracebacks are never stored.
    """
    database_error = find_database_error(exc)
    if database_error is not None:
        # The outer message is dropped too: it may have been built from the database error.
        return f"{type(exc).__name__}: {describe_database_error(database_error)}"
    try:
        text = str(exc)
    except Exception:
        text = "<exception str() failed>"
    message = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    message = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", message).replace("\x00", "")
    if len(message) > MAX_ERROR_LENGTH:
        message = message[: MAX_ERROR_LENGTH - len(_TRUNCATED)] + _TRUNCATED
    return message


def _validate_worker_id(worker_id: str) -> None:
    if not worker_id or len(worker_id) > MAX_WORKER_ID_LENGTH:
        raise ValueError(f"worker_id must be 1-{MAX_WORKER_ID_LENGTH} characters")


def _lease_for(job_type: str, registry: JobRegistry, settings: Settings) -> int:
    handler = registry.get(job_type) if job_type in registry else None
    return (handler.lease_seconds if handler else None) or settings.job_default_lease_seconds


def _forbid_handler_commit(session: Session) -> None:
    @event.listens_for(session, "before_commit")
    def _reject(session_: Session) -> None:
        if not session_.info.get("job_completion"):
            raise HandlerCommitError("Job handlers must not commit; the worker commits on success")


def _log_extra(job: JobRead, worker_id: str, **extra: Any) -> dict[str, Any]:
    return {"job_id": str(job.id), "job_type": job.type, "attempt": job.attempts, "worker_id": worker_id, **extra}


# --- Claim -----------------------------------------------------------------------------------


def claim_next_job(
    worker_id: str, *, registry: JobRegistry = default_registry, settings: Settings | None = None
) -> JobRead | None:
    """Claim the next due job in its own committed transaction. Returns ``None`` if none is due."""
    _validate_worker_id(worker_id)
    settings = settings or get_settings()
    with get_session_factory()() as session, session.begin():
        row = repository.claim_next_job(
            session, worker_id=worker_id, lease_seconds=settings.job_default_lease_seconds
        )
        if row is None:
            return None
        lease = _lease_for(row.type, registry, settings)
        if lease != settings.job_default_lease_seconds:
            row = repository.set_lease(session, job_id=row.id, worker_id=worker_id, lease_seconds=lease)
            assert row is not None
    job = JobRead.model_validate(row)
    logger.info("Job claimed", extra=_log_extra(job, worker_id))
    return job


# --- Execute ---------------------------------------------------------------------------------


def execute_claimed_job(
    job: JobRead,
    worker_id: str,
    *,
    registry: JobRegistry = default_registry,
    settings: Settings | None = None,
    rng: random.Random | None = None,
) -> JobOutcome:
    """Run the handler for a job this worker has claimed and record the outcome."""
    settings = settings or get_settings()

    try:
        handler = registry.get(job.type)
        payload = validate_payload(handler, job.payload)
    except (UnknownJobTypeError, InvalidJobPayloadError) as exc:
        return _record_failure(job, worker_id, exc, permanent=True, settings=settings, rng=rng)

    try:
        succeeded = _run_handler_and_complete(job, worker_id, handler, payload)
    except Exception as exc:
        return _record_failure(
            job, worker_id, exc, permanent=isinstance(exc, PermanentJobError), settings=settings, rng=rng
        )

    if not succeeded:
        logger.warning("Job ownership lost; handler changes rolled back", extra=_log_extra(job, worker_id))
        return JobOutcome.LOST_OWNERSHIP
    logger.info("Job succeeded", extra=_log_extra(job, worker_id))
    return JobOutcome.SUCCEEDED


def _run_handler_and_complete(job: JobRead, worker_id: str, handler: JobHandler[Any], payload: Any) -> bool:
    """Run the handler and mark the job succeeded in one transaction. False if ownership was lost.

    Any exception propagates after the transaction (and so all handler writes) is rolled back.
    """
    with get_session_factory()() as session:
        _forbid_handler_commit(session)
        try:
            context = JobContext(
                session=session,
                job_id=job.id,
                job_type=job.type,
                attempt=job.attempts,
                max_attempts=job.max_attempts,
            )
            handler.func(context, payload)
            # Flush handler writes first so their errors are attributed to the handler.
            session.flush()
            if not repository.mark_succeeded(session, job_id=job.id, worker_id=worker_id, attempt=job.attempts):
                session.rollback()
                return False
            session.info["job_completion"] = True
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise


def _record_failure(
    job: JobRead,
    worker_id: str,
    exc: BaseException,
    *,
    permanent: bool,
    settings: Settings,
    rng: random.Random | None,
) -> JobOutcome:
    error = format_error(exc)
    retry = not permanent and job.attempts < job.max_attempts
    delay = (
        compute_backoff_seconds(
            job.attempts,
            base_seconds=settings.job_backoff_base_seconds,
            max_seconds=settings.job_backoff_max_seconds,
            rng=rng,
        )
        if retry
        else None
    )
    logger.debug("Job attempt failed", exc_info=exc, extra=_log_extra(job, worker_id))

    with get_session_factory()() as session, session.begin():
        row = repository.record_failure(
            session, job_id=job.id, worker_id=worker_id, attempt=job.attempts, error=error, retry_delay_seconds=delay
        )

    if row is None:
        logger.warning("Job ownership lost; failure not recorded", extra=_log_extra(job, worker_id, error=error))
        return JobOutcome.LOST_OWNERSHIP
    if row.status == JobStatus.QUEUED:
        logger.warning(
            "Job attempt failed; retry scheduled",
            extra=_log_extra(job, worker_id, error=error, retry_delay_seconds=round(delay or 0, 3)),
        )
        return JobOutcome.RETRY_SCHEDULED
    logger.error(
        "Job failed", extra=_log_extra(job, worker_id, error=error, permanent=permanent or not retry)
    )
    return JobOutcome.FAILED


def run_next_job(
    worker_id: str,
    *,
    registry: JobRegistry = default_registry,
    settings: Settings | None = None,
    rng: random.Random | None = None,
) -> tuple[JobRead, JobOutcome] | None:
    """Claim and execute one due job. Returns ``None`` if no job was due."""
    job = claim_next_job(worker_id, registry=registry, settings=settings)
    if job is None:
        return None
    return job, execute_claimed_job(job, worker_id, registry=registry, settings=settings, rng=rng)


# --- Recovery --------------------------------------------------------------------------------


def recover_stale_jobs(*, limit: int = 100) -> list[RecoveredJob]:
    """Release up to ``limit`` running jobs whose lease has expired, in one committed transaction."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    with get_session_factory()() as session, session.begin():
        rows: list[Row[Any]] = repository.recover_stale_jobs(session, limit=limit)
    recovered = [RecoveredJob(id=row.id, status=row.status, previous_worker=row.previous_worker) for row in rows]
    for job in recovered:
        logger.warning(
            "Stale job recovered",
            extra={"job_id": str(job.id), "status": job.status.value, "previous_worker": job.previous_worker},
        )
    return recovered
