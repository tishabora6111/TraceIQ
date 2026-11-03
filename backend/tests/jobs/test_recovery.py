"""Stale-job recovery and late-worker protection."""

import threading
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory
from app.modules.jobs.execution import claim_next_job, execute_claimed_job, recover_stale_jobs, run_next_job
from app.modules.jobs.registry import JobContext, JobRegistry
from app.modules.jobs.schemas import JobOutcome, JobStatus
from tests.jobs.helpers import EmptyPayload, MarkerPayload, db_now, enqueue_committed, expire_lease, job_row, markers

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("markers_table")]


def _noop(context: JobContext, payload: EmptyPayload) -> None:
    pass


def _write_marker(context: JobContext, marker: str) -> None:
    context.session.execute(text("INSERT INTO test_job_markers (marker) VALUES (:m)"), {"m": marker})


@pytest.fixture
def registry() -> JobRegistry:
    registry = JobRegistry()
    registry.register("tests.noop", _noop, payload=EmptyPayload)
    registry.register("tests.noop_once", _noop, payload=EmptyPayload, max_attempts=1)
    return registry


def _claim(registry: JobRegistry, worker_id: str = "w1") -> uuid.UUID:
    job = claim_next_job(worker_id, registry=registry)
    assert job is not None
    return job.id


# --- Recovery --------------------------------------------------------------------------------


def test_expired_job_with_attempts_left_is_requeued(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.noop")
    _claim(registry)
    expire_lease(job_id)

    recovered = recover_stale_jobs()

    assert [(job.id, job.status, job.previous_worker) for job in recovered] == [(job_id, JobStatus.QUEUED, "w1")]
    row = job_row(job_id)
    assert row.status == "queued"
    assert row.attempts == 1  # the lost attempt still counts
    assert (row.locked_by, row.locked_at, row.lease_expires_at, row.finished_at) == (None, None, None, None)
    assert row.last_error == "Lease expired before the job finished"
    assert row.run_at <= db_now()

    reclaimed = claim_next_job("w2", registry=registry)
    assert reclaimed is not None and reclaimed.id == job_id and reclaimed.attempts == 2


def test_expired_job_without_attempts_left_is_failed(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.noop_once")
    _claim(registry)
    expire_lease(job_id)

    recovered = recover_stale_jobs()

    assert [(job.id, job.status) for job in recovered] == [(job_id, JobStatus.FAILED)]
    row = job_row(job_id)
    assert (row.status, row.attempts, row.max_attempts) == ("failed", 1, 1)
    assert row.finished_at is not None
    assert (row.locked_by, row.locked_at, row.lease_expires_at) == (None, None, None)
    assert claim_next_job("w2", registry=registry) is None


def test_unexpired_lease_is_not_recovered(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.noop")
    _claim(registry)

    assert recover_stale_jobs() == []
    row = job_row(job_id)
    assert (row.status, row.locked_by) == ("running", "w1")


def test_only_expired_jobs_are_recovered(registry: JobRegistry) -> None:
    expired = enqueue_committed(registry, "tests.noop")
    active = enqueue_committed(registry, "tests.noop")
    queued = enqueue_committed(registry, "tests.noop")
    _claim(registry, "w1")
    _claim(registry, "w2")
    expire_lease(expired)

    recovered = recover_stale_jobs()

    assert [job.id for job in recovered] == [expired]
    assert job_row(active).status == "running"
    assert job_row(queued).status == "queued"


def test_recovery_respects_limit(registry: JobRegistry) -> None:
    job_ids = [enqueue_committed(registry, "tests.noop") for _ in range(5)]
    for _ in job_ids:
        _claim(registry)
    for job_id in job_ids:
        expire_lease(job_id)

    assert len(recover_stale_jobs(limit=2)) == 2
    assert len(recover_stale_jobs(limit=10)) == 3
    assert recover_stale_jobs() == []


def test_recovery_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError):
        recover_stale_jobs(limit=0)


def test_recovery_skips_locked_rows_without_blocking(registry: JobRegistry) -> None:
    """A row locked by another transaction (e.g. a worker completing it) is skipped, not waited on."""
    job_id = enqueue_committed(registry, "tests.noop")
    _claim(registry)
    expire_lease(job_id)

    with get_session_factory()() as locker:
        locker.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": job_id})
        result: dict[str, object] = {}
        thread = threading.Thread(target=lambda: result.update(recovered=recover_stale_jobs()))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "recovery blocked on a locked row"
        locker.rollback()

    assert result["recovered"] == []
    assert job_row(job_id).status == "running"


def test_concurrent_recovery_recovers_each_job_once(registry: JobRegistry) -> None:
    job_ids = {enqueue_committed(registry, "tests.noop") for _ in range(40)}
    for _ in job_ids:
        _claim(registry)
    with get_session_factory()() as session, session.begin():
        session.execute(text("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 minute'"))

    runners = 6
    results: list[list[uuid.UUID]] = [[] for _ in range(runners)]
    errors: list[BaseException] = []
    start = threading.Barrier(runners)

    def recover(index: int) -> None:
        try:
            start.wait()
            while batch := recover_stale_jobs(limit=3):
                results[index].extend(job.id for job in batch)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=recover, args=(i,)) for i in range(runners)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    recovered = [job_id for batch in results for job_id in batch]
    assert len(recovered) == len(job_ids), "a job was recovered more than once or not at all"
    assert set(recovered) == job_ids
    for job_id in job_ids:
        row = job_row(job_id)
        assert (row.status, row.attempts, row.locked_by) == ("queued", 1, None)


# --- Late-worker protection ------------------------------------------------------------------


def _late_worker_registry(on_first_run: Callable[[JobContext], None], *, fail: bool = False) -> JobRegistry:
    """A handler that, on its first run only, lets ``on_first_run`` take the job away mid-execution."""
    registry = JobRegistry()
    runs: list[int] = []

    @registry.handler("tests.late", payload=MarkerPayload)
    def late(context: JobContext, payload: MarkerPayload) -> None:
        runs.append(context.attempt)
        _write_marker(context, f"{payload.marker}-attempt{context.attempt}")
        if len(runs) == 1:
            on_first_run(context)
            if fail:
                raise RuntimeError("late failure")

    return registry


def _recover_and_reclaim(registry_holder: dict[str, JobRegistry]) -> Callable[[JobContext], None]:
    def take_over(context: JobContext) -> None:
        # Simulate worker w1 stalling past its lease: recovery runs and w2 claims the job.
        expire_lease(context.job_id)
        assert [job.id for job in recover_stale_jobs()] == [context.job_id]
        reclaimed = claim_next_job("w2", registry=registry_holder["registry"])
        assert reclaimed is not None and reclaimed.id == context.job_id and reclaimed.attempts == 2

    return take_over


def test_late_worker_cannot_complete_and_its_writes_are_rolled_back() -> None:
    holder: dict[str, JobRegistry] = {}
    registry = holder["registry"] = _late_worker_registry(_recover_and_reclaim(holder))
    job_id = enqueue_committed(registry, "tests.late", {"marker": "m"})
    stale = claim_next_job("w1", registry=registry)
    assert stale is not None

    outcome = execute_claimed_job(stale, "w1", registry=registry)

    assert outcome is JobOutcome.LOST_OWNERSHIP
    # w1's handler write was rolled back; the job now belongs to w2 and is untouched by w1.
    assert markers() == []
    row = job_row(job_id)
    assert (row.status, row.locked_by, row.attempts, row.finished_at) == ("running", "w2", 2, None)

    # The current owner (w2, which claimed attempt 2 above) completes normally; only its writes commit.
    w2_job = stale.model_copy(update={"attempts": 2, "locked_by": "w2"})
    assert execute_claimed_job(w2_job, "w2", registry=registry) is JobOutcome.SUCCEEDED
    assert markers() == ["m-attempt2"]
    assert job_row(job_id).status == "succeeded"


def test_late_worker_cannot_record_failure() -> None:
    holder: dict[str, JobRegistry] = {}
    registry = holder["registry"] = _late_worker_registry(_recover_and_reclaim(holder), fail=True)
    job_id = enqueue_committed(registry, "tests.late", {"marker": "m"})
    stale = claim_next_job("w1", registry=registry)
    assert stale is not None

    outcome = execute_claimed_job(stale, "w1", registry=registry)

    assert outcome is JobOutcome.LOST_OWNERSHIP
    assert markers() == []
    row = job_row(job_id)
    # w1's failure was not recorded: no retry scheduled, error not overwritten, w2 still owns it.
    assert (row.status, row.locked_by, row.attempts) == ("running", "w2", 2)
    assert row.last_error == "Lease expired before the job finished"


def test_worker_with_expired_lease_cannot_complete_before_recovery() -> None:
    """Even before recovery runs, an expired lease means the worker no longer owns the job."""
    registry = _late_worker_registry(lambda context: expire_lease(context.job_id))
    job_id = enqueue_committed(registry, "tests.late", {"marker": "m"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.LOST_OWNERSHIP
    assert markers() == []
    row = job_row(job_id)
    assert (row.status, row.locked_by, row.attempts) == ("running", "w1", 1)
    # Recovery then requeues it, and the next attempt succeeds.
    assert [job.id for job in recover_stale_jobs()] == [job_id]
    result = run_next_job("w2", registry=registry)
    assert result is not None and result[1] is JobOutcome.SUCCEEDED
    assert markers() == ["m-attempt2"]


def test_worker_with_expired_lease_cannot_record_failure() -> None:
    registry = _late_worker_registry(lambda context: expire_lease(context.job_id), fail=True)
    job_id = enqueue_committed(registry, "tests.late", {"marker": "m"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.LOST_OWNERSHIP
    row = job_row(job_id)
    assert (row.status, row.locked_by, row.last_error) == ("running", "w1", None)


def test_completion_waits_for_concurrent_recovery_and_then_fails() -> None:
    """Recovery holding the row lock makes a concurrent completion wait, then see the lost ownership."""
    events: dict[str, threading.Event] = {"locked": threading.Event(), "release": threading.Event()}

    def recover_while_holding_lock() -> None:
        with get_session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL,"
                    " lease_expires_at = NULL, last_error = 'Lease expired before the job finished'"
                    " WHERE status = 'running'"
                )
            )
            events["locked"].set()
            events["release"].wait(timeout=10)

    def start_recovery(context: JobContext) -> None:
        threading.Thread(target=recover_while_holding_lock, daemon=True).start()
        assert events["locked"].wait(timeout=10)
        # Let the recovery commit shortly after the completion starts waiting on the row lock.
        threading.Timer(0.3, events["release"].set).start()

    registry = _late_worker_registry(start_recovery)
    job_id = enqueue_committed(registry, "tests.late", {"marker": "m"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.LOST_OWNERSHIP
    assert markers() == []
    assert job_row(job_id).status == "queued"
