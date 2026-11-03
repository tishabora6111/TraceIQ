import logging
import random
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.modules.jobs.errors import PermanentJobError
from app.modules.jobs.execution import (
    MAX_ERROR_LENGTH,
    claim_next_job,
    compute_backoff_seconds,
    execute_claimed_job,
    format_error,
    run_next_job,
)
from app.modules.jobs.registry import JobContext, JobRegistry
from app.modules.jobs.schemas import JobOutcome
from tests.jobs.helpers import MarkerPayload, db_now, enqueue_committed, job_row, make_due, markers

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("markers_table")]

SECRET_URL = "postgresql://traceiq:s3cr3t-pw@db:5432/traceiq"
# Allows for small steps in the database host clock between transactions (see helpers.CLOCK_MARGIN).
CLOCK_TOLERANCE = timedelta(seconds=2)


def _write_marker(context: JobContext, payload: MarkerPayload) -> None:
    context.session.execute(text("INSERT INTO test_job_markers (marker) VALUES (:m)"), {"m": payload.marker})


@pytest.fixture
def calls() -> list[JobContext]:
    return []


@pytest.fixture
def registry(calls: list[JobContext]) -> JobRegistry:
    registry = JobRegistry()

    @registry.handler("tests.write", payload=MarkerPayload)
    def write(context: JobContext, payload: MarkerPayload) -> None:
        calls.append(context)
        _write_marker(context, payload)

    @registry.handler("tests.write_then_fail", payload=MarkerPayload, max_attempts=3)
    def write_then_fail(context: JobContext, payload: MarkerPayload) -> None:
        calls.append(context)
        _write_marker(context, payload)
        raise RuntimeError("boom")

    @registry.handler("tests.flaky", payload=MarkerPayload)
    def flaky(context: JobContext, payload: MarkerPayload) -> None:
        calls.append(context)
        _write_marker(context, MarkerPayload(marker=f"{payload.marker}-{context.attempt}"))
        if context.attempt == 1:
            raise RuntimeError("transient")

    @registry.handler("tests.permanent", payload=MarkerPayload)
    def permanent(context: JobContext, payload: MarkerPayload) -> None:
        calls.append(context)
        _write_marker(context, payload)
        raise PermanentJobError("cannot succeed")

    @registry.handler("tests.commits", payload=MarkerPayload)
    def commits(context: JobContext, payload: MarkerPayload) -> None:
        _write_marker(context, payload)
        context.session.commit()

    @registry.handler("tests.bad_write", payload=MarkerPayload)
    def bad_write(context: JobContext, payload: MarkerPayload) -> None:
        context.session.execute(text("INSERT INTO test_job_markers (marker) VALUES (NULL)"))

    @registry.handler("tests.leaky_database_error", payload=MarkerPayload)
    def leaky_database_error(context: JobContext, payload: MarkerPayload) -> None:
        # A check violation: PostgreSQL's DETAIL line echoes the whole row, including the marker.
        context.session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (gen_random_uuid(), :email, :hash)"),
            {"email": f"UPPER-{payload.marker}@example.com", "hash": f"hash-{payload.marker}"},
        )

    @registry.handler("tests.leaky_error", payload=MarkerPayload)
    def leaky_error(context: JobContext, payload: MarkerPayload) -> None:
        raise RuntimeError(f"could not connect to {SECRET_URL}\x00" + "x" * 5000)

    @registry.handler("tests.interrupted", payload=MarkerPayload)
    def interrupted(context: JobContext, payload: MarkerPayload) -> None:
        _write_marker(context, payload)
        raise KeyboardInterrupt

    @registry.handler("tests.visibility", payload=MarkerPayload)
    def visibility(context: JobContext, payload: MarkerPayload) -> None:
        _write_marker(context, payload)
        context.session.flush()
        # Neither the handler's write nor the success status is visible outside its transaction yet.
        calls.append(context)
        assert markers() == []
        assert job_row(context.job_id).status == "running"

    return registry


# --- Success ---------------------------------------------------------------------------------


def test_success_commits_handler_writes_with_status(registry: JobRegistry, calls: list[JobContext]) -> None:
    job_id = enqueue_committed(registry, "tests.write", {"marker": "done"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.SUCCEEDED
    assert markers() == ["done"]
    row = job_row(job_id)
    assert row.status == "succeeded"
    assert row.attempts == 1
    assert row.finished_at is not None and row.finished_at >= row.started_at
    assert row.last_error is None
    assert (row.locked_by, row.locked_at, row.lease_expires_at) == (None, None, None)
    # The handler received a fresh session and its job's context.
    (context,) = calls
    assert (context.job_id, context.job_type, context.attempt, context.max_attempts) == (job_id, "tests.write", 1, 5)


def test_handler_writes_invisible_until_completion(registry: JobRegistry, calls: list[JobContext]) -> None:
    enqueue_committed(registry, "tests.visibility", {"marker": "v"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.SUCCEEDED
    assert len(calls) == 1
    assert markers() == ["v"]


def test_execution_uses_fresh_session_not_claim_transaction(registry: JobRegistry, calls: list[JobContext]) -> None:
    enqueue_committed(registry, "tests.write", {"marker": "a"})
    job = claim_next_job("w1", registry=registry)
    assert job is not None
    # The claim is already committed and visible before execution starts.
    assert job_row(job.id).status == "running"

    assert execute_claimed_job(job, "w1", registry=registry) is JobOutcome.SUCCEEDED


def test_no_due_job_returns_none(registry: JobRegistry) -> None:
    assert run_next_job("w1", registry=registry) is None


# --- Failure and retry -----------------------------------------------------------------------


def test_failure_rolls_back_writes_and_schedules_retry(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.write_then_fail", {"marker": "x"})
    before = db_now()

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    assert markers() == []
    row = job_row(job_id)
    assert row.status == "queued"
    assert row.attempts == 1
    assert row.last_error == "RuntimeError: boom"
    assert row.finished_at is None
    assert (row.locked_by, row.locked_at, row.lease_expires_at) == (None, None, None)
    # Backoff for attempt 1 with base 10s: between 5s and 10s from the failure.
    after = db_now()
    assert before + timedelta(seconds=5) - CLOCK_TOLERANCE <= row.run_at <= after + timedelta(seconds=10) + CLOCK_TOLERANCE
    # Not claimable until the backoff has elapsed.
    assert claim_next_job("w1", registry=registry) is None


def test_retry_exhaustion_counts_initial_attempt(registry: JobRegistry, calls: list[JobContext]) -> None:
    job_id = enqueue_committed(registry, "tests.write_then_fail", {"marker": "x"})  # max_attempts=3

    outcomes = []
    for _ in range(3):
        result = run_next_job("w1", registry=registry)
        assert result is not None
        outcomes.append(result[1])
        make_due(job_id)

    assert outcomes == [JobOutcome.RETRY_SCHEDULED, JobOutcome.RETRY_SCHEDULED, JobOutcome.FAILED]
    assert [context.attempt for context in calls] == [1, 2, 3]
    row = job_row(job_id)
    assert (row.status, row.attempts, row.max_attempts) == ("failed", 3, 3)
    assert row.finished_at is not None
    assert row.last_error == "RuntimeError: boom"
    assert markers() == []
    assert run_next_job("w1", registry=registry) is None


def test_retry_then_success(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.flaky", {"marker": "f"})

    first = run_next_job("w1", registry=registry)
    make_due(job_id)
    second = run_next_job("w2", registry=registry)

    assert first is not None and first[1] is JobOutcome.RETRY_SCHEDULED
    assert second is not None and second[1] is JobOutcome.SUCCEEDED
    # Only the successful attempt's writes were committed.
    assert markers() == ["f-2"]
    row = job_row(job_id)
    assert (row.status, row.attempts, row.last_error) == ("succeeded", 2, None)


def test_default_max_attempts_is_five(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.flaky", {"marker": "f"})
    assert job_row(job_id).max_attempts == 5


def test_permanent_error_fails_without_retry(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.permanent", {"marker": "p"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.FAILED
    row = job_row(job_id)
    assert (row.status, row.attempts, row.max_attempts) == ("failed", 1, 5)
    assert row.last_error == "PermanentJobError: cannot succeed"
    assert row.finished_at is not None
    assert markers() == []


def test_unknown_job_type_fails_without_running(registry: JobRegistry, calls: list[JobContext]) -> None:
    job_id = enqueue_committed(registry, "tests.write", {"marker": "u"})
    with get_session_factory()() as session, session.begin():
        session.execute(text("UPDATE jobs SET type = 'tests.removed' WHERE id = :id"), {"id": job_id})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.FAILED
    row = job_row(job_id)
    assert (row.status, row.attempts) == ("failed", 1)
    assert row.last_error.startswith("UnknownJobTypeError:")
    assert calls == []


def test_invalid_stored_payload_fails_without_running(registry: JobRegistry, calls: list[JobContext]) -> None:
    job_id = enqueue_committed(registry, "tests.write", {"marker": "i"})
    with get_session_factory()() as session, session.begin():
        session.execute(text("""UPDATE jobs SET payload = '{"marker": 5}' WHERE id = :id"""), {"id": job_id})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.FAILED
    row = job_row(job_id)
    assert row.status == "failed"
    assert row.last_error.startswith("InvalidJobPayloadError:")
    assert calls == []


def test_handler_cannot_commit_its_own_transaction(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.commits", {"marker": "c"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    assert markers() == []
    assert job_row(job_id).last_error.startswith("HandlerCommitError:")


def test_handler_database_error_is_rolled_back_and_retried(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.bad_write", {"marker": "b"})

    result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    row = job_row(job_id)
    assert row.status == "queued"
    assert row.last_error.startswith("IntegrityError:")


def test_base_exception_propagates_and_leaves_job_for_recovery(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.interrupted", {"marker": "k"})

    with pytest.raises(KeyboardInterrupt):
        run_next_job("w1", registry=registry)

    assert markers() == []
    row = job_row(job_id)
    assert (row.status, row.locked_by) == ("running", "w1")


def test_backoff_uses_configured_settings(registry: JobRegistry) -> None:
    job_id = enqueue_committed(registry, "tests.write_then_fail", {"marker": "x"})
    settings = get_settings().model_copy(update={"job_backoff_base_seconds": 100, "job_backoff_max_seconds": 150})
    for _ in range(2):  # attempt 2: min(100 * 2, 150) = 150 -> delay in [75, 150]
        before = db_now()
        run_next_job("w1", registry=registry, settings=settings)
        if job_row(job_id).attempts == 1:
            make_due(job_id)

    row = job_row(job_id)
    assert row.attempts == 2
    assert before + timedelta(seconds=75) - CLOCK_TOLERANCE <= row.run_at <= db_now() + timedelta(seconds=150) + CLOCK_TOLERANCE


# --- Error recording -------------------------------------------------------------------------


def test_error_is_sanitized_and_truncated(registry: JobRegistry, caplog: pytest.LogCaptureFixture) -> None:
    job_id = enqueue_committed(registry, "tests.leaky_error", {"marker": "l"})

    with caplog.at_level(logging.DEBUG, logger="app.modules.jobs"):
        run_next_job("w1", registry=registry)

    error = job_row(job_id).last_error
    assert "s3cr3t-pw" not in error
    assert "postgresql://traceiq:***@db:5432/traceiq" in error
    assert "\x00" not in error
    assert len(error) <= MAX_ERROR_LENGTH and error.endswith("[truncated]")
    # Non-debug log records carry only the sanitized message.
    for record in caplog.records:
        if record.levelno > logging.DEBUG:
            assert "s3cr3t-pw" not in record.getMessage()
            assert "s3cr3t-pw" not in str(getattr(record, "error", ""))


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("boom"), "RuntimeError: boom"),
        (ValueError(), "ValueError"),
        (RuntimeError("redis://:hunter2@cache:6379/0"), "RuntimeError: redis://:***@cache:6379/0"),
        (RuntimeError("https://user:p@ss:word@example.com/x"), "RuntimeError: https://user:***@example.com/x"),
        (RuntimeError("see https://example.com:8443/path"), "RuntimeError: see https://example.com:8443/path"),
    ],
)
def test_format_error(exc: Exception, expected: str) -> None:
    assert format_error(exc) == expected


DB_MARKER = "job-row-leak-marker-5d1a"


def test_database_error_rows_never_reach_last_error_or_logs(
    registry: JobRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.logging import JsonFormatter

    job_id = enqueue_committed(registry, "tests.leaky_database_error", {"marker": DB_MARKER})

    with caplog.at_level(logging.DEBUG):
        result = run_next_job("w1", registry=registry)

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    error = job_row(job_id).last_error
    assert DB_MARKER not in error and "Failing row" not in error
    assert error == (
        "IntegrityError: <database error message redacted>"
        " db_error_type=CheckViolation sqlstate=23514 constraint=ck_users_email_normalized"
    )
    # Production log output (the JSON formatter) carries no row values, including the DEBUG
    # record's traceback. (pytest's own formatter, caplog.text, is not used in production.)
    assert caplog.records
    for record in caplog.records:
        assert DB_MARKER not in JsonFormatter().format(record)
        if record.levelno > logging.DEBUG:
            assert DB_MARKER not in record.getMessage()
            assert DB_MARKER not in str(getattr(record, "error", ""))


def _chained_database_error() -> BaseException:
    import psycopg
    from sqlalchemy.exc import IntegrityError

    orig = psycopg.errors.CheckViolation(f"violates\nDETAIL:  Failing row contains ({DB_MARKER}).")
    try:
        try:
            raise IntegrityError("INSERT ...", {}, orig)
        except IntegrityError as exc:
            raise RuntimeError(f"save failed: {exc}") from exc
    except RuntimeError as outer:
        return outer


def test_format_error_redacts_errors_caused_by_database_errors() -> None:
    message = format_error(_chained_database_error())

    assert DB_MARKER not in message
    assert message == (
        "RuntimeError: <database error message redacted> db_error_type=CheckViolation sqlstate=23514 constraint=None"
    )


def test_format_error_survives_a_broken_str() -> None:
    class Broken(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str failed")

    assert format_error(Broken()) == "Broken: <exception str() failed>"


def test_format_error_truncates() -> None:
    message = format_error(RuntimeError("y" * 10_000))
    assert len(message) == MAX_ERROR_LENGTH
    assert message.endswith("… [truncated]")


# --- Backoff ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "low", "high"),
    [(1, 5, 10), (2, 10, 20), (3, 20, 40), (9, 1280, 2560), (10, 1800, 3600), (11, 1800, 3600), (1000, 1800, 3600)],
)
def test_backoff_bounds(attempt: int, low: float, high: float) -> None:
    rng = random.Random(1234)
    delays = [compute_backoff_seconds(attempt, base_seconds=10, max_seconds=3600, rng=rng) for _ in range(500)]

    assert all(low <= delay <= high for delay in delays)
    # Jitter actually spreads the delays.
    assert max(delays) - min(delays) > (high - low) * 0.5


def test_backoff_jitter_extremes() -> None:
    class Fixed(random.Random):
        def __init__(self, value: float) -> None:
            super().__init__()
            self.value = value

        def uniform(self, a: float, b: float) -> float:
            return a + (b - a) * self.value

    assert compute_backoff_seconds(3, base_seconds=10, max_seconds=3600, rng=Fixed(0.0)) == 20
    assert compute_backoff_seconds(3, base_seconds=10, max_seconds=3600, rng=Fixed(1.0)) == 40


def test_backoff_rejects_invalid_attempt() -> None:
    with pytest.raises(ValueError):
        compute_backoff_seconds(0, base_seconds=10, max_seconds=3600)
