import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.modules.jobs.execution import claim_next_job
from app.modules.jobs.registry import JobContext, JobRegistry
from app.modules.jobs.schemas import JobStatus
from app.modules.jobs.service import enqueue
from tests.jobs.helpers import EmptyPayload, db_now, enqueue_committed, job_row

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]


def _noop(context: JobContext, payload: EmptyPayload) -> None:
    pass


@pytest.fixture
def registry() -> JobRegistry:
    registry = JobRegistry()
    registry.register("tests.noop", _noop, payload=EmptyPayload)
    registry.register("tests.long", _noop, payload=EmptyPayload, lease_seconds=1800)
    return registry


def test_claim_returns_none_when_no_jobs(registry: JobRegistry) -> None:
    assert claim_next_job("w1", registry=registry) is None


def test_claim_marks_job_running_and_commits(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.noop")

    claimed = claim_next_job("w1", registry=registry)

    assert claimed is not None and claimed.id == job_id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_by == "w1"
    assert claimed.locked_at is not None and claimed.started_at == claimed.locked_at
    # The claim is committed before returning: an independent session sees it.
    row = job_row(job_id)
    assert row.status == "running" and row.locked_by == "w1" and row.attempts == 1


def test_default_lease_from_settings(registry: JobRegistry) -> None:
    enqueue_committed(registry, "tests.noop")
    settings = get_settings().model_copy(update={"job_default_lease_seconds": 42})

    claimed = claim_next_job("w1", registry=registry, settings=settings)

    assert claimed is not None and claimed.lease_expires_at and claimed.locked_at
    assert claimed.lease_expires_at - claimed.locked_at == timedelta(seconds=42)


def test_handler_lease_override(registry: JobRegistry) -> None:
    enqueue_committed(registry, "tests.long")

    claimed = claim_next_job("w1", registry=registry)

    assert claimed is not None and claimed.lease_expires_at and claimed.locked_at
    assert claimed.lease_expires_at - claimed.locked_at == timedelta(seconds=1800)
    assert job_row(claimed.id).lease_expires_at == claimed.lease_expires_at


def test_unknown_type_is_claimed_with_default_lease(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.noop")
    with get_session_factory()() as session, session.begin():
        session.execute(text("UPDATE jobs SET type = 'tests.removed' WHERE id = :id"), {"id": job_id})

    claimed = claim_next_job("w1", registry=registry)

    assert claimed is not None and claimed.lease_expires_at and claimed.locked_at
    assert claimed.lease_expires_at - claimed.locked_at == timedelta(seconds=300)


def test_job_not_claimed_before_run_at(registry: JobRegistry) -> None:
    enqueue_committed(registry, "tests.noop", run_at=db_now() + timedelta(hours=1))

    assert claim_next_job("w1", registry=registry) is None


def test_claim_order_is_run_at_then_created_at(registry: JobRegistry) -> None:
    now = db_now()
    late = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=1))
    early = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=10))
    same_time_first = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=5))
    same_time_second = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=5))
    future = enqueue_committed(registry, "tests.noop", run_at=now + timedelta(hours=1))

    claimed = [claim_next_job("w1", registry=registry) for _ in range(5)]

    assert [job.id if job else None for job in claimed] == [early, same_time_first, same_time_second, late, None]
    assert job_row(future).status == "queued"


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_non_queued_jobs_are_not_claimed(registry: JobRegistry, status: str) -> None:
    job_id = enqueue_committed(registry, "tests.noop")
    claim_next_job("w1", registry=registry)
    if status != "running":
        with get_session_factory()() as session, session.begin():
            session.execute(
                text(
                    f"UPDATE jobs SET status = '{status}', finished_at = now(), locked_by = NULL,"
                    " locked_at = NULL, lease_expires_at = NULL WHERE id = :id"
                ),
                {"id": job_id},
            )

    assert claim_next_job("w2", registry=registry) is None


def test_uncommitted_job_is_not_claimable(registry: JobRegistry) -> None:
    with get_session_factory()() as session:
        enqueue(session, "tests.noop", {}, registry=registry)
        assert claim_next_job("w1", registry=registry) is None
        session.rollback()


def test_locked_job_is_skipped_without_blocking(registry: JobRegistry) -> None:
    """A row locked by another transaction is skipped (SKIP LOCKED), not waited on."""
    now = db_now()
    first = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=2))
    second = enqueue_committed(registry, "tests.noop", run_at=now - timedelta(minutes=1))

    with get_session_factory()() as locker:
        locker.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": first})
        result: dict[str, object] = {}
        thread = threading.Thread(target=lambda: result.update(job=claim_next_job("w1", registry=registry)))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "claim blocked on a locked row"
        locker.rollback()

    claimed = result["job"]
    assert claimed is not None and claimed.id == second  # type: ignore[attr-defined]
    assert job_row(first).status == "queued"


@pytest.mark.parametrize("worker_id", ["", "w" * 256])
def test_invalid_worker_id_is_rejected(registry: JobRegistry, worker_id: str) -> None:
    with pytest.raises(ValueError, match="worker_id"):
        claim_next_job(worker_id, registry=registry)


def test_concurrent_workers_claim_each_job_exactly_once(registry: JobRegistry) -> None:
    job_ids = {enqueue_committed(registry, "tests.noop") for _ in range(60)}
    workers = 8
    claimed: dict[str, list[uuid.UUID]] = {f"w{i}": [] for i in range(workers)}
    errors: list[BaseException] = []
    start = threading.Barrier(workers)

    def work(worker_id: str) -> None:
        try:
            start.wait()
            while (job := claim_next_job(worker_id, registry=registry)) is not None:
                claimed[worker_id].append(job.id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(worker_id,)) for worker_id in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    all_claims = [job_id for ids in claimed.values() for job_id in ids]
    assert len(all_claims) == len(job_ids), "a job was claimed more than once or not at all"
    assert set(all_claims) == job_ids
    # More than one worker took part (the claims were genuinely concurrent).
    assert sum(1 for ids in claimed.values() if ids) > 1
    for worker_id, ids in claimed.items():
        for job_id in ids:
            row = job_row(job_id)
            assert (row.status, row.attempts, row.locked_by) == ("running", 1, worker_id)
