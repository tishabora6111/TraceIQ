"""Schedule synchronization and scheduler ticks."""

import logging
import threading
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.database import get_session_factory
from app.modules.jobs import repository
from app.modules.jobs.execution import claim_next_job, run_next_job
from app.modules.jobs.models import Job
from app.modules.jobs.registry import JobContext, JobPayload, JobRegistry
from app.modules.jobs.scheduler import run_scheduler_tick, sync_schedules
from app.modules.jobs.schemas import ScheduleRunOutcome, ScheduleSyncResult
from tests.jobs.helpers import db_now, job_row

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]


class WindowPayload(JobPayload):
    window_minutes: int = 5


def _noop(context: JobContext, payload: WindowPayload) -> None:
    pass


def make_registry(schedules: dict[str, dict[str, Any]] | None = None) -> JobRegistry:
    """A registry with job type ``tests.detect`` and the given schedules (name -> options)."""
    registry = JobRegistry()
    registry.register("tests.detect", _noop, payload=WindowPayload)
    for name, options in (schedules or {}).items():
        registry.register_schedule(name, options.get("job_type", "tests.detect"), **{
            key: value for key, value in options.items() if key != "job_type"
        })  # fmt: skip
    return registry


def schedule_row(name: str) -> Any:
    with get_session_factory()() as session:
        return session.execute(text("SELECT * FROM job_schedules WHERE name = :n"), {"n": name}).one()


def set_schedule(name: str, **assignments: str) -> None:
    """Set schedule columns to SQL expressions, e.g. next_run_at="now() - interval '1 hour'"."""
    sql = ", ".join(f"{column} = {expression}" for column, expression in assignments.items())
    with get_session_factory()() as session, session.begin():
        session.execute(text(f"UPDATE job_schedules SET {sql} WHERE name = :n"), {"n": name})


def sync_due(registry: JobRegistry) -> None:
    """Synchronize, then make every enabled schedule due by a clear margin (see CLOCK_MARGIN)."""
    sync_schedules(registry)
    with get_session_factory()() as session, session.begin():
        session.execute(text("UPDATE job_schedules SET next_run_at = now() - interval '10 seconds' WHERE enabled"))


def count_jobs() -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(Job)) or 0


def jobs_for(name: str) -> list[Any]:
    with get_session_factory()() as session:
        return list(
            session.execute(
                text("SELECT * FROM jobs WHERE dedupe_key = :k ORDER BY created_at"), {"k": f"schedule:{name}"}
            ).all()
        )


# --- Synchronization -------------------------------------------------------------------------


def test_sync_creates_new_schedules_due_immediately() -> None:
    registry = make_registry({"tests.hourly": {"interval": 3600}, "tests.fast": {"interval": 30}})
    before = db_now()

    result = sync_schedules(registry)

    assert result == ScheduleSyncResult(created=("tests.fast", "tests.hourly"))
    row = schedule_row("tests.hourly")
    assert (row.job_type, row.interval_seconds, row.payload, row.enabled) == ("tests.detect", 3600, {"window_minutes": 5}, True)
    assert row.last_enqueued_at is None
    # Due immediately: next_run_at is the creating transaction's now(), i.e. its created_at.
    assert row.next_run_at == row.created_at
    assert abs(row.next_run_at - before) < timedelta(seconds=5)


def test_resync_without_changes_touches_nothing() -> None:
    registry = make_registry({"tests.hourly": {"interval": 3600}})
    sync_schedules(registry)
    set_schedule("tests.hourly", next_run_at="now() + interval '30 minutes'")
    before = schedule_row("tests.hourly")

    result = sync_schedules(make_registry({"tests.hourly": {"interval": 3600}}))

    assert result == ScheduleSyncResult()
    after = schedule_row("tests.hourly")
    assert (after.next_run_at, after.updated_at) == (before.next_run_at, before.updated_at)


def test_payload_change_updates_without_resetting_next_run() -> None:
    sync_schedules(make_registry({"tests.hourly": {"interval": 3600}}))
    set_schedule("tests.hourly", next_run_at="now() + interval '30 minutes'")
    before = schedule_row("tests.hourly")

    result = sync_schedules(make_registry({"tests.hourly": {"interval": 3600, "payload": {"window_minutes": 15}}}))

    assert result == ScheduleSyncResult(updated=("tests.hourly",))
    after = schedule_row("tests.hourly")
    assert after.payload == {"window_minutes": 15}
    assert after.next_run_at == before.next_run_at
    assert after.updated_at > before.updated_at


def test_job_type_change_updates_without_resetting_next_run() -> None:
    sync_schedules(make_registry({"tests.hourly": {"interval": 3600}}))
    set_schedule("tests.hourly", next_run_at="now() + interval '30 minutes'")
    before = schedule_row("tests.hourly")
    registry = make_registry()
    registry.register("tests.other", _noop, payload=WindowPayload)
    registry.register_schedule("tests.hourly", "tests.other", interval=3600)

    assert sync_schedules(registry) == ScheduleSyncResult(updated=("tests.hourly",))
    after = schedule_row("tests.hourly")
    assert (after.job_type, after.next_run_at) == ("tests.other", before.next_run_at)


def test_longer_interval_keeps_next_run() -> None:
    sync_schedules(make_registry({"tests.s": {"interval": 60}}))
    set_schedule("tests.s", next_run_at="now() + interval '30 seconds'", last_enqueued_at="now() - interval '30 seconds'")
    before = schedule_row("tests.s")

    sync_schedules(make_registry({"tests.s": {"interval": 3600}}))

    after = schedule_row("tests.s")
    assert after.interval_seconds == 3600
    assert after.next_run_at == before.next_run_at


def test_shorter_interval_brings_next_run_earlier() -> None:
    sync_schedules(make_registry({"tests.s": {"interval": 86400}}))
    set_schedule("tests.s", next_run_at="now() + interval '20 hours'", last_enqueued_at="now() - interval '4 hours'")
    before = schedule_row("tests.s")

    sync_schedules(make_registry({"tests.s": {"interval": 3600}}))

    after = schedule_row("tests.s")
    # One new interval after the last run, i.e. already due, instead of waiting 20 hours.
    assert after.next_run_at == before.last_enqueued_at + timedelta(hours=1)
    assert after.next_run_at <= db_now()


def test_shorter_interval_for_never_run_schedule_uses_now() -> None:
    sync_schedules(make_registry({"tests.s": {"interval": 86400}}))
    set_schedule("tests.s", next_run_at="now() + interval '20 hours'")
    before_sync = db_now()

    sync_schedules(make_registry({"tests.s": {"interval": 60}}))

    next_run_at = schedule_row("tests.s").next_run_at
    assert before_sync <= next_run_at <= db_now() + timedelta(seconds=60)


def test_removed_schedule_is_disabled_not_deleted() -> None:
    sync_due(make_registry({"tests.keep": {"interval": 60}, "tests.remove": {"interval": 60}}))
    runs = run_scheduler_tick(make_registry({"tests.keep": {"interval": 60}, "tests.remove": {"interval": 60}}))
    assert len(runs) == 2
    history = schedule_row("tests.remove")

    result = sync_schedules(make_registry({"tests.keep": {"interval": 60}}))

    assert result == ScheduleSyncResult(disabled=("tests.remove",))
    row = schedule_row("tests.remove")
    assert row.enabled is False
    assert (row.last_enqueued_at, row.next_run_at, row.payload) == (
        history.last_enqueued_at, history.next_run_at, history.payload,
    )  # fmt: skip
    assert len(jobs_for("tests.remove")) == 1  # its job history is kept
    assert schedule_row("tests.keep").enabled is True


def test_disabled_schedule_is_not_run_and_can_be_re_enabled() -> None:
    sync_schedules(make_registry({"tests.s": {"interval": 60}}))
    sync_schedules(make_registry())
    set_schedule("tests.s", next_run_at="now() - interval '1 hour'")

    assert run_scheduler_tick(make_registry({"tests.s": {"interval": 60}})) == []

    result = sync_schedules(make_registry({"tests.s": {"interval": 60}}))
    assert result == ScheduleSyncResult(updated=("tests.s",))
    assert schedule_row("tests.s").enabled is True
    # Overdue after re-enabling: runs once on the next tick.
    assert [run.outcome for run in run_scheduler_tick(make_registry({"tests.s": {"interval": 60}}))] == [
        ScheduleRunOutcome.ENQUEUED
    ]


def test_empty_registry_disables_all_schedules() -> None:
    sync_schedules(make_registry({"tests.a": {"interval": 60}, "tests.b": {"interval": 60}}))

    assert sync_schedules(make_registry()) == ScheduleSyncResult(disabled=("tests.a", "tests.b"))
    assert sync_schedules(make_registry()) == ScheduleSyncResult()


def test_sync_rolls_back_entirely_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_schedules(make_registry({"tests.old": {"interval": 60}}))
    original = repository.upsert_schedule

    def failing_upsert(session: Any, **kwargs: Any) -> str | None:
        if kwargs["name"] == "tests.b":
            raise RuntimeError("database went away")
        return original(session, **kwargs)

    monkeypatch.setattr(repository, "upsert_schedule", failing_upsert)
    with pytest.raises(RuntimeError):
        sync_schedules(make_registry({"tests.a": {"interval": 60}, "tests.b": {"interval": 60}}))

    with get_session_factory()() as session:
        names = list(session.scalars(text("SELECT name FROM job_schedules ORDER BY name")))
    assert names == ["tests.old"]
    assert schedule_row("tests.old").enabled is True


def test_concurrent_startup_sync_from_multiple_workers() -> None:
    definitions = {f"tests.s{i}": {"interval": 60 + i} for i in range(10)}
    workers = 6
    results: list[ScheduleSyncResult] = []
    errors: list[BaseException] = []
    start = threading.Barrier(workers)

    def worker_startup() -> None:
        try:
            registry = make_registry(definitions)
            start.wait()
            results.append(sync_schedules(registry))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker_startup) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    created = Counter(name for result in results for name in result.created)
    # Each schedule was created exactly once; every other worker found it already up to date.
    assert created == Counter(list(definitions))
    assert all(not result.updated and not result.disabled for result in results)
    with get_session_factory()() as session:
        rows = session.execute(text("SELECT name, interval_seconds, enabled FROM job_schedules ORDER BY name")).all()
    assert {(row.name, row.interval_seconds, row.enabled) for row in rows} == {
        (name, options["interval"], True) for name, options in definitions.items()
    }


# --- Ticks -----------------------------------------------------------------------------------


def test_due_schedule_enqueues_job_and_advances() -> None:
    registry = make_registry({"tests.s": {"interval": 60, "payload": {"window_minutes": 2}}})
    sync_schedules(registry)
    set_schedule("tests.s", next_run_at="now() - interval '10 seconds'")
    before = schedule_row("tests.s")

    runs = run_scheduler_tick(registry)

    assert len(runs) == 1
    run = runs[0]
    assert (run.name, run.outcome) == ("tests.s", ScheduleRunOutcome.ENQUEUED)
    job = job_row(run.job_id)
    assert (job.type, job.status, job.payload, job.dedupe_key, job.max_attempts) == (
        "tests.detect", "queued", {"window_minutes": 2}, "schedule:tests.s", 5,
    )  # fmt: skip
    after = schedule_row("tests.s")
    assert after.next_run_at == before.next_run_at + timedelta(seconds=60) == run.next_run_at
    assert after.last_enqueued_at is not None and after.last_enqueued_at >= job.created_at


def test_new_schedule_runs_on_first_tick() -> None:
    registry = make_registry({"tests.s": {"interval": 3600}})
    sync_due(registry)

    assert [run.outcome for run in run_scheduler_tick(registry)] == [ScheduleRunOutcome.ENQUEUED]


def test_not_yet_due_schedule_is_not_run() -> None:
    registry = make_registry({"tests.s": {"interval": 60}})
    sync_schedules(registry)
    set_schedule("tests.s", next_run_at="now() + interval '10 seconds'")
    before = schedule_row("tests.s")

    assert run_scheduler_tick(registry) == []
    assert count_jobs() == 0
    assert schedule_row("tests.s").next_run_at == before.next_run_at


def test_no_catch_up_burst_after_downtime() -> None:
    registry = make_registry({"tests.s": {"interval": 60}})
    sync_schedules(registry)
    # 61 runs were missed (one hour and ten seconds of downtime at a 60-second interval).
    set_schedule("tests.s", next_run_at="now() - interval '1 hour 10 seconds'")
    old = schedule_row("tests.s").next_run_at

    runs = run_scheduler_tick(registry)
    now = db_now()

    assert [run.outcome for run in runs] == [ScheduleRunOutcome.ENQUEUED]
    assert count_jobs() == 1
    new = schedule_row("tests.s").next_run_at
    # The next run is the first slot after now on the original grid.
    assert now < new <= now + timedelta(seconds=60)
    assert (new - old).total_seconds() % 60 == 0
    # Nothing more is due: the backlog is not replayed.
    assert run_scheduler_tick(registry) == []
    assert count_jobs() == 1


@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_active_previous_run_skips_new_run(active_status: str) -> None:
    registry = make_registry({"tests.s": {"interval": 60}})
    sync_due(registry)
    first = run_scheduler_tick(registry)[0]
    if active_status == "running":
        assert claim_next_job("w1", registry=registry) is not None
    set_schedule("tests.s", next_run_at="now() - interval '10 seconds'")
    before = schedule_row("tests.s")

    runs = run_scheduler_tick(registry)

    assert [(run.outcome, run.job_id) for run in runs] == [(ScheduleRunOutcome.SKIPPED_ACTIVE, None)]
    assert [job.id for job in jobs_for("tests.s")] == [first.job_id]
    after = schedule_row("tests.s")
    # The skipped run still advances the schedule, but is not recorded as an enqueue.
    assert after.next_run_at == before.next_run_at + timedelta(seconds=60)
    assert after.last_enqueued_at == before.last_enqueued_at


def test_new_run_enqueued_after_previous_finishes() -> None:
    registry = make_registry({"tests.s": {"interval": 60}})
    sync_due(registry)
    first = run_scheduler_tick(registry)[0]
    result = run_next_job("w1", registry=registry)
    assert result is not None and result[0].id == first.job_id
    set_schedule("tests.s", next_run_at="now() - interval '10 seconds'")

    second = run_scheduler_tick(registry)[0]

    assert second.outcome is ScheduleRunOutcome.ENQUEUED and second.job_id != first.job_id
    assert [job.status for job in jobs_for("tests.s")] == ["succeeded", "queued"]


def test_unknown_job_type_in_this_worker_is_skipped_without_advancing() -> None:
    sync_due(make_registry({"tests.s": {"interval": 60}}))
    before = schedule_row("tests.s")
    other_worker = JobRegistry()  # e.g. an older deployment without this job type

    runs = run_scheduler_tick(other_worker)

    assert [run.outcome for run in runs] == [ScheduleRunOutcome.SKIPPED_UNKNOWN_TYPE]
    assert count_jobs() == 0
    assert schedule_row("tests.s").next_run_at == before.next_run_at


def test_invalid_stored_payload_is_skipped_without_advancing() -> None:
    registry = make_registry({"tests.s": {"interval": 60}})
    sync_due(registry)
    set_schedule("tests.s", payload="""'{"window_minutes": "soon"}'::jsonb""")
    before = schedule_row("tests.s")

    runs = run_scheduler_tick(registry)

    assert [run.outcome for run in runs] == [ScheduleRunOutcome.SKIPPED_INVALID_PAYLOAD]
    assert count_jobs() == 0
    assert schedule_row("tests.s").next_run_at == before.next_run_at


def test_tick_respects_limit() -> None:
    registry = make_registry({f"tests.s{i}": {"interval": 60} for i in range(5)})
    sync_due(registry)

    assert len(run_scheduler_tick(registry, limit=2)) == 2
    assert len(run_scheduler_tick(registry, limit=10)) == 3
    assert run_scheduler_tick(registry) == []
    with pytest.raises(ValueError):
        run_scheduler_tick(registry, limit=0)


def test_tick_rolls_back_entirely_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = make_registry({"tests.a": {"interval": 60}, "tests.b": {"interval": 60}})
    sync_due(registry)
    before = {name: schedule_row(name).next_run_at for name in ("tests.a", "tests.b")}
    original = repository.advance_schedule

    def failing_advance(session: Any, **kwargs: Any) -> None:
        if kwargs["name"] == "tests.b":
            raise RuntimeError("database went away")
        original(session, **kwargs)

    monkeypatch.setattr(repository, "advance_schedule", failing_advance)
    with pytest.raises(RuntimeError):
        run_scheduler_tick(registry)

    # Neither schedule advanced and no job (not even tests.a's) was committed.
    assert count_jobs() == 0
    assert {name: schedule_row(name).next_run_at for name in before} == before
    monkeypatch.undo()
    assert len(run_scheduler_tick(registry)) == 2


def _run_concurrently(target: Callable[[], None], threads: int) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(threads)

    def run() -> None:
        try:
            start.wait()
            target()
        except BaseException as exc:  # pragma: no cover - surfaced by callers
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    return errors


def test_concurrent_ticks_enqueue_each_due_schedule_once() -> None:
    definitions = {f"tests.s{i}": {"interval": 60} for i in range(12)}
    registry = make_registry(definitions)
    sync_schedules(registry)
    for name in definitions:
        set_schedule(name, next_run_at="now() - interval '10 seconds'")
    before = {name: schedule_row(name).next_run_at for name in definitions}
    runs: list[Any] = []
    lock = threading.Lock()

    def tick() -> None:
        for _ in range(3):
            result = run_scheduler_tick(registry, limit=4)
            with lock:
                runs.extend(result)

    errors = _run_concurrently(tick, threads=8)

    assert not errors
    enqueued = Counter(run.name for run in runs if run.outcome is ScheduleRunOutcome.ENQUEUED)
    assert enqueued == Counter(list(definitions)), "a schedule was enqueued more than once or not at all"
    assert all(run.outcome is ScheduleRunOutcome.ENQUEUED for run in runs)
    assert count_jobs() == len(definitions)
    for name in definitions:
        # Each schedule advanced exactly once (no lost or doubled update).
        assert schedule_row(name).next_run_at == before[name] + timedelta(seconds=60)
        assert len(jobs_for(name)) == 1


def test_concurrent_ticks_with_sync() -> None:
    """Startup synchronization and ticks running together neither deadlock nor duplicate jobs."""
    definitions = {f"tests.s{i}": {"interval": 60} for i in range(8)}
    registry = make_registry(definitions)
    sync_due(registry)

    def tick_or_sync() -> None:
        for _ in range(3):
            run_scheduler_tick(registry)
            sync_schedules(make_registry(definitions))

    errors = _run_concurrently(tick_or_sync, threads=6)

    assert not errors
    assert count_jobs() == len(definitions)
    for name in definitions:
        assert len(jobs_for(name)) == 1


# --- Logging ---------------------------------------------------------------------------------


def test_sync_and_tick_log_structured_records(caplog: pytest.LogCaptureFixture) -> None:
    registry = make_registry({"tests.s": {"interval": 60}})

    with caplog.at_level(logging.INFO, logger="app.modules.jobs.scheduler"):
        sync_due(registry)
        run_scheduler_tick(registry)

    sync_record = next(record for record in caplog.records if record.getMessage() == "Schedules synchronized")
    assert sync_record.schedules_created == ["tests.s"]  # type: ignore[attr-defined]
    tick_record = next(record for record in caplog.records if record.getMessage() == "Scheduled run")
    assert (tick_record.schedule, tick_record.outcome) == ("tests.s", "enqueued")  # type: ignore[attr-defined]
