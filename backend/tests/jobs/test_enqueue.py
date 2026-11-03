import enum
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.modules.jobs.errors import DuplicateJobError, InvalidJobPayloadError, UnknownJobTypeError
from app.modules.jobs.models import Job
from app.modules.jobs.registry import JobContext, JobPayload, JobRegistry
from app.modules.jobs.schemas import JobRead, JobStatus
from app.modules.jobs.service import enqueue, get_job

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]


class Color(enum.Enum):
    RED = "red"


class Inner(JobPayload):
    label: str


class EchoPayload(JobPayload):
    message: str
    count: int = 1


class RichPayload(JobPayload):
    trace_id: uuid.UUID
    observed_at: datetime
    day: date
    amount: Decimal
    color: Color
    inner: Inner
    tags: list[str]
    pair: tuple[int, int]
    ratio: float
    note: str | None = None


class OtherPayload(JobPayload):
    message: str


def _noop(context: JobContext, payload: JobPayload) -> None:
    pass


@pytest.fixture
def registry() -> JobRegistry:
    registry = JobRegistry()
    registry.register("tests.echo", _noop, payload=EchoPayload)
    registry.register("tests.rich", _noop, payload=RichPayload)
    registry.register("tests.limited", _noop, payload=EchoPayload, max_attempts=2)
    return registry


@pytest.fixture
def session() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session


def _count_jobs() -> int:
    """Count committed jobs from an independent session."""
    with get_session_factory()() as other:
        return other.scalar(select(func.count()).select_from(Job)) or 0


# --- Validation ----------------------------------------------------------------------------


def test_unknown_job_type_is_rejected(session: Session, registry: JobRegistry) -> None:
    with pytest.raises(UnknownJobTypeError):
        enqueue(session, "tests.missing", {}, registry=registry)
    session.commit()
    assert _count_jobs() == 0


def test_valid_mapping_payload_is_stored(session: Session, registry: JobRegistry) -> None:
    job = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry)
    session.commit()

    assert job.payload == {"message": "hi", "count": 1}
    stored = get_job(session, job.id)
    assert stored is not None and stored.payload == {"message": "hi", "count": 1}


def test_valid_model_payload_is_stored(session: Session, registry: JobRegistry) -> None:
    job = enqueue(session, "tests.echo", EchoPayload(message="hi", count=3), registry=registry)

    assert job.payload == {"message": "hi", "count": 3}


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({}, "missing"),
        ({"message": 123}, "string_type"),
        ({"message": "hi", "count": "many"}, "int_parsing"),
        ({"message": "hi", "unexpected": True}, "extra_forbidden"),
        (None, "missing"),
    ],
)
def test_invalid_payload_is_rejected(
    session: Session, registry: JobRegistry, payload: object, error_type: str
) -> None:
    with pytest.raises(InvalidJobPayloadError) as exc_info:
        enqueue(session, "tests.echo", payload, registry=registry)  # type: ignore[arg-type]

    assert exc_info.value.code == "invalid_job_payload"
    assert error_type in {error["type"] for error in exc_info.value.details}
    session.commit()
    assert _count_jobs() == 0


def test_payload_of_wrong_model_is_rejected(session: Session, registry: JobRegistry) -> None:
    with pytest.raises(InvalidJobPayloadError, match="expected EchoPayload, got OtherPayload"):
        enqueue(session, "tests.echo", OtherPayload(message="hi"), registry=registry)


def test_unvalidated_model_instance_is_revalidated(session: Session, registry: JobRegistry) -> None:
    bypassed = EchoPayload.model_construct(message=123, count=1)

    with pytest.raises(InvalidJobPayloadError):
        enqueue(session, "tests.echo", bypassed, registry=registry)


def test_supported_values_are_serialized_to_json(session: Session, registry: JobRegistry) -> None:
    trace_id = uuid.uuid4()
    payload = RichPayload(
        trace_id=trace_id,
        observed_at=datetime(2026, 11, 3, 12, 30, tzinfo=UTC),
        day=date(2026, 11, 3),
        amount=Decimal("12.50"),
        color=Color.RED,
        inner=Inner(label="x"),
        tags=["a", "b"],
        pair=(1, 2),
        ratio=0.25,
    )

    job = enqueue(session, "tests.rich", payload, registry=registry)
    session.commit()

    expected = {
        "trace_id": str(trace_id),
        "observed_at": "2026-11-03T12:30:00Z",
        "day": "2026-11-03",
        "amount": "12.50",
        "color": "red",
        "inner": {"label": "x"},
        "tags": ["a", "b"],
        "pair": [1, 2],
        "ratio": 0.25,
        "note": None,
    }
    assert job.payload == expected
    # Read back the raw JSONB from the database.
    stored = session.scalar(text("SELECT payload FROM jobs WHERE id = :id"), {"id": job.id})
    assert stored == expected
    # The stored payload validates back into the handler's model.
    assert RichPayload.model_validate(stored) == payload


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_json_float_values_are_rejected(session: Session, registry: JobRegistry, value: float) -> None:
    payload = {
        "trace_id": uuid.uuid4(),
        "observed_at": datetime.now(UTC),
        "day": date.today(),
        "amount": "1",
        "color": "red",
        "inner": {"label": "x"},
        "tags": [],
        "pair": [1, 2],
        "ratio": value,
    }
    with pytest.raises(InvalidJobPayloadError, match="not JSON-serializable"):
        enqueue(session, "tests.rich", payload, registry=registry)


# --- Transaction ownership -----------------------------------------------------------------


def test_job_visible_only_after_caller_commits(session: Session, registry: JobRegistry) -> None:
    job = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry)

    assert session.in_transaction()
    assert _count_jobs() == 0
    session.commit()
    assert _count_jobs() == 1
    assert get_job(session, job.id) is not None


def test_job_discarded_when_caller_rolls_back(session: Session, registry: JobRegistry) -> None:
    job = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry)
    session.rollback()

    assert _count_jobs() == 0
    assert get_job(session, job.id) is None


def test_enqueue_joins_callers_existing_work(session: Session, registry: JobRegistry) -> None:
    first = enqueue(session, "tests.echo", {"message": "one"}, registry=registry)
    second = enqueue(session, "tests.echo", {"message": "two"}, registry=registry)
    session.rollback()

    assert get_job(session, first.id) is None
    assert get_job(session, second.id) is None


# --- Defaults and options ------------------------------------------------------------------


def test_job_fields_on_creation(session: Session, registry: JobRegistry) -> None:
    before = session.scalar(text("SELECT clock_timestamp()"))
    job = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry)

    assert isinstance(job, JobRead)
    assert job.id.version == 4
    assert job.type == "tests.echo"
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.dedupe_key is None
    assert job.locked_by is None and job.lease_expires_at is None
    assert job.started_at is None and job.finished_at is None and job.last_error is None
    # run_at defaults to the database's now() (start of this transaction).
    assert job.run_at.tzinfo is not None
    assert abs((job.run_at - before).total_seconds()) < 5


def test_default_max_attempts_from_settings(session: Session, registry: JobRegistry) -> None:
    assert enqueue(session, "tests.echo", {"message": "hi"}, registry=registry).max_attempts == 5

    custom = get_settings().model_copy(update={"job_default_max_attempts": 7})
    job = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry, settings=custom)
    assert job.max_attempts == 7


def test_handler_max_attempts_override(session: Session, registry: JobRegistry) -> None:
    custom: Settings = get_settings().model_copy(update={"job_default_max_attempts": 7})

    job = enqueue(session, "tests.limited", {"message": "hi"}, registry=registry, settings=custom)
    assert job.max_attempts == 2


def test_custom_run_at(session: Session, registry: JobRegistry) -> None:
    run_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))

    job = enqueue(session, "tests.echo", {"message": "hi"}, run_at=run_at, registry=registry)
    session.commit()

    assert job.run_at == run_at
    stored = get_job(session, job.id)
    assert stored is not None and stored.run_at == run_at


def test_naive_run_at_is_rejected(session: Session, registry: JobRegistry) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        enqueue(session, "tests.echo", {"message": "hi"}, run_at=datetime(2030, 1, 1), registry=registry)


@pytest.mark.parametrize("key", ["", "k" * 256])
def test_invalid_dedupe_key_is_rejected(session: Session, registry: JobRegistry, key: str) -> None:
    with pytest.raises(ValueError, match="dedupe_key"):
        enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key=key, registry=registry)


# --- Deduplication -------------------------------------------------------------------------


def _set_status(session: Session, job_id: uuid.UUID, status: JobStatus) -> None:
    """Move a job to another status with fields consistent with the table's constraints."""
    if status is JobStatus.RUNNING:
        sql = (
            "UPDATE jobs SET status = 'running', attempts = 1, locked_by = 'w', locked_at = now(),"
            " lease_expires_at = now() + interval '5 minutes', started_at = now() WHERE id = :id"
        )
    else:
        sql = (
            f"UPDATE jobs SET status = '{status.value}', attempts = 1, started_at = now(),"
            " finished_at = now() WHERE id = :id"
        )
    session.execute(text(sql), {"id": job_id})


def test_dedupe_key_is_stored(session: Session, registry: JobRegistry) -> None:
    job = enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)

    assert job.dedupe_key == "k1"


@pytest.mark.parametrize("active_status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_active_duplicate_is_rejected(session: Session, registry: JobRegistry, active_status: JobStatus) -> None:
    original = enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)
    if active_status is JobStatus.RUNNING:
        _set_status(session, original.id, JobStatus.RUNNING)
    session.commit()

    with pytest.raises(DuplicateJobError) as exc_info:
        enqueue(session, "tests.echo", {"message": "again"}, dedupe_key="k1", registry=registry)

    assert exc_info.value.existing_job_id == original.id
    assert exc_info.value.details == {"dedupe_key": "k1", "existing_job_id": str(original.id)}
    session.commit()
    assert _count_jobs() == 1


def test_duplicate_does_not_break_callers_transaction(session: Session, registry: JobRegistry) -> None:
    enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)
    session.commit()

    other = enqueue(session, "tests.echo", {"message": "other"}, registry=registry)
    with pytest.raises(DuplicateJobError):
        enqueue(session, "tests.echo", {"message": "again"}, dedupe_key="k1", registry=registry)
    # The transaction is still usable: earlier work commits, later work succeeds.
    later = enqueue(session, "tests.echo", {"message": "later"}, registry=registry)
    session.commit()

    assert get_job(session, other.id) is not None
    assert get_job(session, later.id) is not None
    assert _count_jobs() == 3


def test_duplicate_within_same_uncommitted_transaction_is_rejected(
    session: Session, registry: JobRegistry
) -> None:
    first = enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)

    with pytest.raises(DuplicateJobError) as exc_info:
        enqueue(session, "tests.echo", {"message": "again"}, dedupe_key="k1", registry=registry)
    assert exc_info.value.existing_job_id == first.id


@pytest.mark.parametrize("terminal_status", [JobStatus.SUCCEEDED, JobStatus.FAILED])
def test_dedupe_key_reusable_after_job_is_terminal(
    session: Session, registry: JobRegistry, terminal_status: JobStatus
) -> None:
    first = enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)
    _set_status(session, first.id, terminal_status)
    session.commit()

    second = enqueue(session, "tests.echo", {"message": "again"}, dedupe_key="k1", registry=registry)
    session.commit()

    assert second.id != first.id
    assert second.status is JobStatus.QUEUED
    assert _count_jobs() == 2


def test_different_dedupe_keys_and_no_key_do_not_conflict(session: Session, registry: JobRegistry) -> None:
    enqueue(session, "tests.echo", {"message": "a"}, dedupe_key="k1", registry=registry)
    enqueue(session, "tests.echo", {"message": "b"}, dedupe_key="k2", registry=registry)
    enqueue(session, "tests.echo", {"message": "c"}, registry=registry)
    enqueue(session, "tests.echo", {"message": "d"}, registry=registry)
    session.commit()

    assert _count_jobs() == 4


@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
def test_concurrent_enqueue_with_same_dedupe_key(registry: JobRegistry, first_outcome: str) -> None:
    """A second transaction waits for the first; it fails if the first commits, succeeds if it rolls back."""
    factory = get_session_factory()
    first_session = factory()
    result: dict[str, object] = {}

    def second_transaction() -> None:
        with factory() as second_session:
            try:
                job = enqueue(second_session, "tests.echo", {"message": "second"}, dedupe_key="k1", registry=registry)
                second_session.commit()
                result["job"] = job
            except DuplicateJobError as exc:
                second_session.rollback()
                result["error"] = exc

    try:
        first = enqueue(first_session, "tests.echo", {"message": "first"}, dedupe_key="k1", registry=registry)
        thread = threading.Thread(target=second_transaction)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "second enqueue should wait for the first transaction"

        if first_outcome == "commit":
            first_session.commit()
        else:
            first_session.rollback()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        first_session.close()

    if first_outcome == "commit":
        assert isinstance(result.get("error"), DuplicateJobError)
        assert result["error"].existing_job_id == first.id  # type: ignore[attr-defined]
    else:
        assert isinstance(result.get("job"), JobRead)
    assert _count_jobs() == 1


# --- Retrieval -----------------------------------------------------------------------------


def test_get_existing_job(session: Session, registry: JobRegistry) -> None:
    created = enqueue(session, "tests.echo", {"message": "hi"}, dedupe_key="k1", registry=registry)
    session.commit()

    with get_session_factory()() as other:
        fetched = get_job(other, created.id)

    assert fetched == created


def test_get_nonexistent_job_returns_none(session: Session) -> None:
    assert get_job(session, uuid.uuid4()) is None


def test_get_job_sees_callers_uncommitted_job(session: Session, registry: JobRegistry) -> None:
    created = enqueue(session, "tests.echo", {"message": "hi"}, registry=registry)

    assert get_job(session, created.id) == created
