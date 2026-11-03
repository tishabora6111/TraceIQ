"""Interval-based recurring jobs.

Schedules are defined in code (``JobRegistry.register_schedule``) and stored in ``job_schedules``.

* ``sync_schedules`` runs at worker startup: it creates or updates the code-defined schedules and
  disables stored schedules no longer defined in code. Concurrent workers serialize on a
  transaction-level advisory lock.
* ``run_scheduler_tick`` runs periodically: it locks due schedules (``FOR UPDATE SKIP LOCKED``),
  enqueues each one's job with dedupe key ``schedule:<name>``, and advances ``next_run_at``,
  all in one transaction. If the previous run is still active, the run is skipped (the schedule
  still advances). All times come from the PostgreSQL clock.
"""

import logging

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.modules.jobs import repository
from app.modules.jobs.errors import DuplicateJobError, InvalidJobPayloadError
from app.modules.jobs.registry import SCHEDULE_DEDUPE_PREFIX, JobRegistry, default_registry
from app.modules.jobs.schemas import ScheduleRun, ScheduleRunOutcome, ScheduleSyncResult
from app.modules.jobs.service import enqueue

logger = logging.getLogger(__name__)

DEFAULT_TICK_LIMIT = 100


def sync_schedules(registry: JobRegistry = default_registry) -> ScheduleSyncResult:
    """Write the registry's schedules to the database in one committed transaction."""
    created: list[str] = []
    updated: list[str] = []
    schedules = registry.schedules()

    with get_session_factory()() as session, session.begin():
        repository.lock_schedule_sync(session)
        for schedule in schedules:  # ordered by name
            change = repository.upsert_schedule(
                session,
                name=schedule.name,
                job_type=schedule.job_type,
                interval_seconds=schedule.interval_seconds,
                payload=schedule.payload,
            )
            if change == "created":
                created.append(schedule.name)
            elif change == "updated":
                updated.append(schedule.name)
        disabled = repository.disable_schedules_except(session, [schedule.name for schedule in schedules])

    result = ScheduleSyncResult(created=tuple(created), updated=tuple(updated), disabled=tuple(sorted(disabled)))
    logger.info(
        "Schedules synchronized",
        extra={
            # Not "created": that name is a built-in LogRecord attribute.
            "schedules_created": list(result.created),
            "schedules_updated": list(result.updated),
            "schedules_disabled": list(result.disabled),
        },
    )
    return result


def run_scheduler_tick(
    registry: JobRegistry = default_registry,
    *,
    settings: Settings | None = None,
    limit: int = DEFAULT_TICK_LIMIT,
) -> list[ScheduleRun]:
    """Enqueue jobs for up to ``limit`` due schedules in one committed transaction.

    If anything fails, the whole transaction rolls back: no job is enqueued and no schedule
    advances, so the next tick retries.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    settings = settings or get_settings()
    runs: list[ScheduleRun] = []

    with get_session_factory()() as session, session.begin():
        for schedule in repository.lock_due_schedules(session, limit=limit):
            if schedule.job_type not in registry:
                runs.append(ScheduleRun(name=schedule.name, outcome=ScheduleRunOutcome.SKIPPED_UNKNOWN_TYPE))
                continue
            try:
                job = enqueue(
                    session,
                    schedule.job_type,
                    schedule.payload,
                    dedupe_key=f"{SCHEDULE_DEDUPE_PREFIX}{schedule.name}",
                    registry=registry,
                    settings=settings,
                )
            except InvalidJobPayloadError:
                runs.append(ScheduleRun(name=schedule.name, outcome=ScheduleRunOutcome.SKIPPED_INVALID_PAYLOAD))
                continue
            except DuplicateJobError:
                outcome, job_id = ScheduleRunOutcome.SKIPPED_ACTIVE, None
            else:
                outcome, job_id = ScheduleRunOutcome.ENQUEUED, job.id

            repository.advance_schedule(
                session,
                name=schedule.name,
                next_run_at=schedule.new_next_run_at,
                enqueued=outcome is ScheduleRunOutcome.ENQUEUED,
            )
            runs.append(
                ScheduleRun(name=schedule.name, outcome=outcome, job_id=job_id, next_run_at=schedule.new_next_run_at)
            )

    for run in runs:
        # Skipping because the previous run is still active is routine; the other skips mean the
        # running code does not match the stored schedule.
        routine = {ScheduleRunOutcome.ENQUEUED, ScheduleRunOutcome.SKIPPED_ACTIVE}
        level = logging.INFO if run.outcome in routine else logging.WARNING
        logger.log(
            level,
            "Scheduled run",
            extra={"schedule": run.name, "outcome": run.outcome.value, "job_id": str(run.job_id) if run.job_id else None},
        )
    return runs
