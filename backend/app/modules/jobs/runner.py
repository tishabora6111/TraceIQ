"""The worker loop.

One ``Worker`` runs in its own process and executes one job at a time. It is synchronous:
idle waiting uses ``threading.Event.wait`` purely as an interruptible sleep (no threads are
started), so a shutdown signal ends the wait immediately.

Lifecycle:

1. ``startup``: check the database and synchronize code-defined schedules. Failures here are
   fatal (``WorkerStartupError``).
2. Loop until stopped: run maintenance when due (stale-job recovery, then one scheduler tick),
   claim and execute the next due job, or wait up to the poll interval if there was none.
   Errors inside the loop (e.g. a database blip) are logged and retried after the poll
   interval; they do not stop the worker.
3. Shutdown: after SIGTERM/SIGINT (or ``request_stop``) no new job is claimed; a job already
   executing runs to completion. A second signal raises ``KeyboardInterrupt`` to force exit; the
   interrupted job's writes are rolled back and lease recovery returns it to the queue.
"""

import logging
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from app.core.config import Settings, get_settings
from app.core.database import check_database_connection
from app.modules.jobs.execution import format_error, recover_stale_jobs, run_next_job
from app.modules.jobs.registry import JobRegistry, default_registry
from app.modules.jobs.scheduler import run_scheduler_tick, sync_schedules
from app.modules.jobs.schemas import JobOutcome, JobRead, RecoveredJob, ScheduleRun, ScheduleSyncResult

logger = logging.getLogger(__name__)

SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)
RECOVERY_BATCH_SIZE = 100
SCHEDULER_BATCH_SIZE = 100


class WorkerStartupError(RuntimeError):
    """The worker could not start (database unreachable, schema missing, schedule sync failed)."""


@dataclass
class MaintenanceResult:
    recovered: list[RecoveredJob] = field(default_factory=list)
    scheduled: list[ScheduleRun] = field(default_factory=list)
    # Sanitized error descriptions; maintenance failures are logged, never raised.
    errors: list[str] = field(default_factory=list)


def default_worker_id() -> str:
    """``<hostname>-<pid>-<random>``: unique per process, readable in logs and the jobs table."""
    return f"{socket.gethostname()[:200]}-{os.getpid()}-{secrets.token_hex(3)}"


class Worker:
    def __init__(
        self,
        *,
        registry: JobRegistry = default_registry,
        settings: Settings | None = None,
        worker_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.settings = settings or get_settings()
        self.worker_id = worker_id or default_worker_id()
        self._clock = clock
        self._stop = threading.Event()
        self._next_maintenance_at: float | None = None  # None: due now

    # --- Control -----------------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self, reason: str = "requested") -> None:
        """Stop claiming new jobs; the current job (if any) finishes. Safe to call repeatedly."""
        if not self._stop.is_set():
            logger.info("Worker shutdown requested", extra={"worker_id": self.worker_id, "reason": reason})
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; returns early (True) when a stop is requested."""
        return self._stop.wait(timeout=max(timeout, 0.0))

    @contextmanager
    def handle_signals(self) -> Iterator[None]:
        """Install SIGTERM/SIGINT handlers for graceful shutdown; restore the previous ones on exit.

        Must be used from the main thread (a Python restriction on ``signal.signal``).
        """
        previous = {signum: signal.getsignal(signum) for signum in SHUTDOWN_SIGNALS}

        def handle(signum: int, frame: FrameType | None) -> None:
            name = signal.Signals(signum).name
            if self.stopping:
                logger.warning(
                    "Second shutdown signal; forcing exit", extra={"worker_id": self.worker_id, "signal": name}
                )
                raise KeyboardInterrupt(name)
            self.request_stop(reason=name)

        for signum in SHUTDOWN_SIGNALS:
            signal.signal(signum, handle)
        try:
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    # --- Steps -------------------------------------------------------------------------------

    def startup(self) -> ScheduleSyncResult:
        """Check the database and synchronize schedules. Raises ``WorkerStartupError``."""
        try:
            check_database_connection()
            result = sync_schedules(self.registry)
        except Exception as exc:
            raise WorkerStartupError(format_error(exc)) from exc
        logger.info(
            "Worker ready",
            extra={
                "worker_id": self.worker_id,
                "handlers": sorted(self.registry.names()),
                "schedules": [schedule.name for schedule in self.registry.schedules()],
                "poll_interval_seconds": self.settings.worker_poll_interval_seconds,
                "maintenance_interval_seconds": self.settings.worker_maintenance_interval_seconds,
            },
        )
        return result

    def run_once(self) -> tuple[JobRead, JobOutcome] | None:
        """Claim and execute one due job. Returns ``None`` if none was due or a stop was requested."""
        if self.stopping:
            return None
        return run_next_job(self.worker_id, registry=self.registry, settings=self.settings)

    def run_maintenance(self) -> MaintenanceResult:
        """Recover stale jobs, then run one scheduler tick. Errors are logged and returned, not raised."""
        self._next_maintenance_at = self._clock() + self.settings.worker_maintenance_interval_seconds
        result = MaintenanceResult()
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("recovery", lambda: result.recovered.extend(recover_stale_jobs(limit=RECOVERY_BATCH_SIZE))),
            (
                "scheduler",
                lambda: result.scheduled.extend(
                    run_scheduler_tick(self.registry, settings=self.settings, limit=SCHEDULER_BATCH_SIZE)
                ),
            ),
        ]
        for step, action in steps:
            try:
                action()
            except Exception as exc:
                error = format_error(exc)
                result.errors.append(f"{step}: {error}")
                logger.error(
                    "Worker maintenance step failed",
                    extra={"worker_id": self.worker_id, "step": step, "error": error},
                )
                logger.debug("Maintenance failure details", exc_info=exc)
        logger.debug(
            "Worker maintenance finished",
            extra={
                "worker_id": self.worker_id,
                "recovered_count": len(result.recovered),
                "scheduled_count": len(result.scheduled),
            },
        )
        return result

    def maintenance_due(self) -> bool:
        return self._next_maintenance_at is None or self._clock() >= self._next_maintenance_at

    def _idle_timeout(self) -> float:
        timeout = self.settings.worker_poll_interval_seconds
        if self._next_maintenance_at is not None:
            timeout = min(timeout, self._next_maintenance_at - self._clock())
        return max(timeout, 0.0)

    # --- Loop --------------------------------------------------------------------------------

    def run(self) -> None:
        """Start up, then process jobs until a stop is requested."""
        self.startup()
        try:
            while not self.stopping:
                if self.maintenance_due():
                    self.run_maintenance()
                    continue  # re-check for a stop before claiming
                try:
                    result = self.run_once()
                except Exception as exc:
                    logger.error(
                        "Worker loop error; retrying after the poll interval",
                        extra={"worker_id": self.worker_id, "error": format_error(exc)},
                    )
                    logger.debug("Worker loop error details", exc_info=exc)
                    self.wait(self.settings.worker_poll_interval_seconds)
                    continue
                if result is None:
                    self.wait(self._idle_timeout())
        finally:
            logger.info("Worker stopped", extra={"worker_id": self.worker_id})
