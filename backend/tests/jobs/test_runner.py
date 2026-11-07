"""Worker loop lifecycle. Test handlers live in per-test registries."""

import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.modules.jobs import runner as runner_module
from app.modules.jobs.execution import claim_next_job
from app.modules.jobs.registry import JobContext, JobRegistry
from app.modules.jobs.runner import SHUTDOWN_SIGNALS, MaintenanceResult, Worker, WorkerStartupError
from app.modules.jobs.schemas import JobOutcome, ScheduleRunOutcome
from tests.jobs.helpers import MarkerPayload, enqueue_committed, expire_lease, job_row, markers

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("markers_table")]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _write_marker(context: JobContext, marker: str) -> None:
    context.session.execute(text("INSERT INTO test_job_markers (marker) VALUES (:m)"), {"m": marker})


def make_registry(on_run: Callable[[JobContext, MarkerPayload], None] | None = None) -> JobRegistry:
    registry = JobRegistry()

    @registry.handler("tests.write", payload=MarkerPayload)
    def write(context: JobContext, payload: MarkerPayload) -> None:
        _write_marker(context, payload.marker)
        if on_run is not None:
            on_run(context, payload)

    @registry.handler("tests.fail", payload=MarkerPayload)
    def fail(context: JobContext, payload: MarkerPayload) -> None:
        _write_marker(context, payload.marker)
        raise RuntimeError("handler failed")

    return registry


def make_worker(registry: JobRegistry, **settings_overrides: Any) -> Worker:
    settings = get_settings().model_copy(
        update={"worker_poll_interval_seconds": 0.01, "worker_maintenance_interval_seconds": 60, **settings_overrides}
    )
    return Worker(registry=registry, settings=settings, worker_id="test-worker")


class RecordingWorker(Worker):
    """Records idle waits and stops after ``max_waits`` of them instead of sleeping."""

    def __init__(self, *args: Any, max_waits: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.waits: list[float] = []
        self.max_waits = max_waits

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.max_waits:
            self.request_stop("test")
        return self.stopping


def recording_worker(registry: JobRegistry, *, max_waits: int = 1, clock: Any = None, **overrides: Any) -> RecordingWorker:
    settings = get_settings().model_copy(
        update={"worker_poll_interval_seconds": 5, "worker_maintenance_interval_seconds": 60, **overrides}
    )
    kwargs: dict[str, Any] = {"registry": registry, "settings": settings, "worker_id": "test-worker"}
    if clock is not None:
        kwargs["clock"] = clock
    return RecordingWorker(max_waits=max_waits, **kwargs)


# --- run_once ------------------------------------------------------------------------------


def test_run_once_success() -> None:
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.write", {"marker": "ok"})

    result = make_worker(registry).run_once()

    assert result is not None
    job, outcome = result
    assert (job.id, outcome) == (job_id, JobOutcome.SUCCEEDED)
    assert job_row(job_id).status == "succeeded"
    assert markers() == ["ok"]


def test_run_once_failure() -> None:
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.fail", {"marker": "x"})

    result = make_worker(registry).run_once()

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    row = job_row(job_id)
    assert (row.status, row.attempts, row.last_error) == ("queued", 1, "RuntimeError: handler failed")
    assert markers() == []


def test_run_once_without_jobs_returns_none() -> None:
    assert make_worker(make_registry()).run_once() is None


def test_run_once_after_stop_claims_nothing() -> None:
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.write", {"marker": "late"})
    worker = make_worker(registry)
    worker.request_stop()

    assert worker.run_once() is None
    assert job_row(job_id).status == "queued"


def test_default_worker_id_is_unique_and_bounded() -> None:
    ids = {Worker(registry=JobRegistry()).worker_id for _ in range(20)}

    assert len(ids) == 20
    assert all(0 < len(worker_id) <= 255 for worker_id in ids)


# --- Startup -------------------------------------------------------------------------------


def test_startup_synchronizes_schedules() -> None:
    registry = make_registry()
    registry.register_schedule("tests.every_minute", "tests.write", interval=60, payload={"marker": "s"})

    result = make_worker(registry).startup()

    assert result.created == ("tests.every_minute",)
    with get_session_factory()() as session:
        assert session.scalar(text("SELECT enabled FROM job_schedules WHERE name = 'tests.every_minute'")) is True


def test_startup_with_empty_registry() -> None:
    result = make_worker(JobRegistry()).startup()

    assert (result.created, result.updated, result.disabled) == ((), (), ())


def test_startup_logs_readiness(caplog: pytest.LogCaptureFixture) -> None:
    registry = make_registry()
    registry.register_schedule("tests.every_minute", "tests.write", interval=60, payload={"marker": "s"})

    with caplog.at_level(logging.INFO, logger="app.modules.jobs.runner"):
        make_worker(registry).startup()

    record = next(r for r in caplog.records if r.getMessage() == "Worker ready")
    assert record.worker_id == "test-worker"  # type: ignore[attr-defined]
    assert record.handlers == ["tests.fail", "tests.write"]  # type: ignore[attr-defined]
    assert record.schedules == ["tests.every_minute"]  # type: ignore[attr-defined]


def test_startup_fails_when_database_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection to postgresql://u:pw@db/x refused"))

    monkeypatch.setattr(runner_module, "check_database_connection", unavailable)

    with pytest.raises(WorkerStartupError, match="OperationalError") as exc_info:
        make_worker(make_registry()).run()
    assert "pw@" not in str(exc_info.value)


def test_startup_fails_when_schedule_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_sync(registry: JobRegistry) -> None:
        raise RuntimeError('relation "job_schedules" does not exist')

    monkeypatch.setattr(runner_module, "sync_schedules", broken_sync)
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.write", {"marker": "never"})

    with pytest.raises(WorkerStartupError, match="job_schedules"):
        make_worker(registry).run()
    # The loop never started: no job was claimed.
    assert job_row(job_id).status == "queued"


# --- Loop: polling and maintenance ---------------------------------------------------------


def test_run_processes_queued_jobs_then_idles() -> None:
    registry = make_registry()
    job_ids = [enqueue_committed(registry, "tests.write", {"marker": f"m{i}"}) for i in range(3)]
    worker = recording_worker(registry, max_waits=1)

    worker.run()

    assert [job_row(job_id).status for job_id in job_ids] == ["succeeded"] * 3
    assert markers() == ["m0", "m1", "m2"]
    # It waited only once, after the queue was empty, for the poll interval.
    assert worker.waits == [5]


def test_idle_wait_is_capped_by_next_maintenance() -> None:
    clock = FakeClock()
    worker = recording_worker(make_registry(), max_waits=2, clock=clock, worker_maintenance_interval_seconds=3)

    worker.run()

    # Maintenance ran at t=1000 and is next due at t=1003: idle waits never overshoot it.
    assert worker.waits == [3, 3]


def test_run_executes_maintenance_on_start_and_then_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    calls: list[float] = []
    original = Worker.run_maintenance

    def recording_maintenance(self: Worker) -> MaintenanceResult:
        calls.append(clock.now)
        return original(self)

    monkeypatch.setattr(Worker, "run_maintenance", recording_maintenance)

    class AdvancingWorker(RecordingWorker):
        def wait(self, timeout: float) -> bool:
            clock.now += timeout  # time passes while idle
            return super().wait(timeout)

    worker = AdvancingWorker(
        registry=make_registry(),
        settings=get_settings().model_copy(
            update={"worker_poll_interval_seconds": 4, "worker_maintenance_interval_seconds": 10}
        ),
        worker_id="test-worker",
        clock=clock,
        max_waits=7,
    )

    worker.run()

    # Idle waits of 4, 4, 2 (capped by the next maintenance) reach t=1010; 4, 4, 2 more reach t=1020,
    # where maintenance runs a third time before the seventh wait stops the worker.
    assert calls == [1000, 1010, 1020]


def test_maintenance_due_and_not_due() -> None:
    clock = FakeClock()
    worker = Worker(
        registry=JobRegistry(),
        settings=get_settings().model_copy(update={"worker_maintenance_interval_seconds": 15}),
        clock=clock,
    )

    assert worker.maintenance_due()  # never ran yet
    worker.run_maintenance()
    assert not worker.maintenance_due()
    clock.now += 14.9
    assert not worker.maintenance_due()
    clock.now += 0.1
    assert worker.maintenance_due()


def test_maintenance_recovers_stale_jobs_and_runs_scheduler() -> None:
    registry = make_registry()
    registry.register_schedule("tests.every_minute", "tests.write", interval=60, payload={"marker": "sched"})
    stale_id = enqueue_committed(registry, "tests.write", {"marker": "stale"})
    assert claim_next_job("crashed-worker", registry=registry) is not None
    expire_lease(stale_id)
    worker = make_worker(registry)
    worker.startup()

    result = worker.run_maintenance()

    assert result.errors == []
    assert [(job.id, job.previous_worker) for job in result.recovered] == [(stale_id, "crashed-worker")]
    assert [(run.name, run.outcome) for run in result.scheduled] == [("tests.every_minute", ScheduleRunOutcome.ENQUEUED)]
    assert job_row(stale_id).status == "queued"


def test_maintenance_errors_are_isolated_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = make_registry()
    registry.register_schedule("tests.every_minute", "tests.write", interval=60, payload={"marker": "sched"})
    make_worker(registry).startup()

    def broken_recovery(limit: int) -> list[Any]:
        raise RuntimeError("recovery broke at postgresql://u:secret-pw@db/x")

    monkeypatch.setattr(runner_module, "recover_stale_jobs", broken_recovery)
    with caplog.at_level(logging.ERROR, logger="app.modules.jobs.runner"):
        result = make_worker(registry).run_maintenance()

    assert result.errors == ["recovery: RuntimeError: recovery broke at postgresql://u:***@db/x"]
    # The scheduler step still ran.
    assert [run.outcome for run in result.scheduled] == [ScheduleRunOutcome.ENQUEUED]
    assert all("secret-pw" not in str(getattr(record, "error", "")) for record in caplog.records)


def test_loop_survives_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.write", {"marker": "after-blip"})
    real_run_next_job = runner_module.run_next_job
    calls = {"n": 0}

    def flaky_run_next_job(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT", {}, Exception("server closed the connection unexpectedly"))
        return real_run_next_job(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_next_job", flaky_run_next_job)
    worker = recording_worker(registry, max_waits=2)

    worker.run()

    # First claim failed (logged, waited one poll interval), then the job ran, then the queue was empty.
    assert job_row(job_id).status == "succeeded"
    assert worker.waits == [5, 5]


# --- Shutdown ------------------------------------------------------------------------------


def test_stop_during_job_lets_it_finish_and_claims_nothing_more() -> None:
    worker_holder: dict[str, Worker] = {}

    def stop_mid_job(context: JobContext, payload: MarkerPayload) -> None:
        worker_holder["worker"].request_stop("test")

    registry = make_registry(on_run=stop_mid_job)
    first = enqueue_committed(registry, "tests.write", {"marker": "first"})
    second = enqueue_committed(registry, "tests.write", {"marker": "second"})
    worker = worker_holder["worker"] = make_worker(registry)

    worker.run()

    assert job_row(first).status == "succeeded"
    assert markers() == ["first"]
    assert job_row(second).status == "queued"


def test_shutdown_while_idle_interrupts_wait_promptly() -> None:
    worker = make_worker(make_registry(), worker_poll_interval_seconds=30)
    threading.Timer(0.3, worker.request_stop).start()  # test-only thread standing in for a signal

    started = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"shutdown waited {elapsed:.1f}s for the 30s poll interval"


def test_request_stop_is_idempotent() -> None:
    worker = make_worker(make_registry())
    worker.request_stop()
    worker.request_stop()

    assert worker.stopping
    assert worker.wait(30) is True  # returns immediately once stopping


def test_run_returns_immediately_if_stopped_before_start() -> None:
    registry = make_registry()
    job_id = enqueue_committed(registry, "tests.write", {"marker": "x"})
    worker = make_worker(registry)
    worker.request_stop()

    worker.run()

    assert job_row(job_id).status == "queued"


# --- Signals -------------------------------------------------------------------------------


def _sentinel_handler(signum: int, frame: object) -> None:  # pragma: no cover - never called
    pass


@pytest.fixture
def sentinel_signal_handlers() -> Any:
    previous = {signum: signal.getsignal(signum) for signum in SHUTDOWN_SIGNALS}
    for signum in SHUTDOWN_SIGNALS:
        signal.signal(signum, _sentinel_handler)
    yield
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@pytest.mark.usefixtures("sentinel_signal_handlers")
def test_signal_handlers_installed_and_restored() -> None:
    worker = make_worker(make_registry())

    with worker.handle_signals():
        for signum in SHUTDOWN_SIGNALS:
            assert signal.getsignal(signum) is not _sentinel_handler
    for signum in SHUTDOWN_SIGNALS:
        assert signal.getsignal(signum) is _sentinel_handler


@pytest.mark.usefixtures("sentinel_signal_handlers")
def test_signal_handlers_restored_after_error() -> None:
    worker = make_worker(make_registry())

    with pytest.raises(RuntimeError), worker.handle_signals():
        raise RuntimeError("boom")
    for signum in SHUTDOWN_SIGNALS:
        assert signal.getsignal(signum) is _sentinel_handler


@pytest.mark.parametrize("signum", SHUTDOWN_SIGNALS)
@pytest.mark.usefixtures("sentinel_signal_handlers")
def test_first_signal_requests_stop_second_forces_exit(signum: signal.Signals) -> None:
    worker = make_worker(make_registry())

    with worker.handle_signals():
        handler = signal.getsignal(signum)
        assert callable(handler)
        handler(signum, None)
        assert worker.stopping
        with pytest.raises(KeyboardInterrupt):
            handler(signum, None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
@pytest.mark.usefixtures("sentinel_signal_handlers")
def test_real_sigterm_during_job_finishes_job_then_stops() -> None:
    def send_sigterm(context: JobContext, payload: MarkerPayload) -> None:
        signal.raise_signal(signal.SIGTERM)

    registry = make_registry(on_run=send_sigterm)
    first = enqueue_committed(registry, "tests.write", {"marker": "first"})
    second = enqueue_committed(registry, "tests.write", {"marker": "second"})
    worker = make_worker(registry)

    with worker.handle_signals():
        worker.run()

    assert job_row(first).status == "succeeded"
    assert job_row(second).status == "queued"
    assert markers() == ["first"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
@pytest.mark.usefixtures("sentinel_signal_handlers")
def test_real_sigterm_while_idle_stops_promptly() -> None:
    worker = make_worker(make_registry(), worker_poll_interval_seconds=30)
    previous_alarm = signal.signal(signal.SIGALRM, lambda *_: signal.raise_signal(signal.SIGTERM))
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.3)
        started = time.monotonic()
        with worker.handle_signals():
            worker.run()
        elapsed = time.monotonic() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)

    assert worker.stopping
    assert elapsed < 5


# --- Sensitive data in logs ----------------------------------------------------------------


SENSITIVE_VALUE = "patient-ssn-123-45-6789"


def test_database_error_parameters_and_payload_never_reach_logs_or_last_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler's database error involving a sensitive value: not in logs, tracebacks or last_error."""
    registry = JobRegistry()

    @registry.handler("tests.sensitive", payload=MarkerPayload)
    def sensitive(context: JobContext, payload: MarkerPayload) -> None:
        # The sensitive payload value is bound as a SQL parameter of a failing statement.
        context.session.execute(
            text("INSERT INTO test_job_markers (marker) SELECT :marker WHERE 1 / 0 = 1"), {"marker": payload.marker}
        )

    job_id = enqueue_committed(registry, "tests.sensitive", {"marker": SENSITIVE_VALUE})

    with caplog.at_level(logging.DEBUG):
        result = make_worker(registry).run_once()

    assert result is not None and result[1] is JobOutcome.RETRY_SCHEDULED
    # Safe diagnostics are kept in the stored error (database messages themselves are redacted)...
    last_error = job_row(job_id).last_error
    assert last_error == (
        "DataError: <database error message redacted> db_error_type=DivisionByZero sqlstate=22012 constraint=None"
    )
    # ...but the value appears nowhere: not in last_error, messages, structured fields, or tracebacks.
    assert SENSITIVE_VALUE not in last_error
    formatter = logging.Formatter()
    assert caplog.records, "expected log records from the job attempt"
    for record in caplog.records:
        assert SENSITIVE_VALUE not in formatter.format(record)
        assert SENSITIVE_VALUE not in repr(record.__dict__)
    # The failure itself was logged with its (sanitized) error.
    failure = next(r for r in caplog.records if r.getMessage() == "Job attempt failed; retry scheduled")
    assert failure.error == last_error  # type: ignore[attr-defined]
