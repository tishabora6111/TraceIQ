"""The ``python -m app.worker`` entry point."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app import worker as worker_entrypoint
from app.modules.jobs.runner import Worker, WorkerStartupError

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --- main() --------------------------------------------------------------------------------


def test_main_returns_zero_after_graceful_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(Worker, "run", lambda self: ran.append(self.worker_id))

    assert worker_entrypoint.main() == worker_entrypoint.EXIT_OK
    assert len(ran) == 1


def test_main_returns_one_on_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Worker) -> None:
        raise WorkerStartupError("OperationalError: database unavailable")

    monkeypatch.setattr(Worker, "run", fail)

    assert worker_entrypoint.main() == worker_entrypoint.EXIT_STARTUP_FAILED


def test_main_returns_130_when_forced_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(self: Worker) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Worker, "run", interrupted)

    assert worker_entrypoint.main() == worker_entrypoint.EXIT_INTERRUPTED


def test_main_disposes_engine_and_restores_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed: list[bool] = []

    class FakeEngine:
        def dispose(self) -> None:
            disposed.append(True)

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(worker_entrypoint, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(Worker, "run", lambda self: None)

    worker_entrypoint.main()

    assert disposed == [True]
    assert {signum: signal.getsignal(signum) for signum in previous} == previous


def test_production_registry_is_used_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def capture(self: Worker) -> None:
        seen["registry"] = self.registry

    monkeypatch.setattr(Worker, "run", capture)
    worker_entrypoint.main()

    from app.modules.jobs.registry import default_registry

    assert seen["registry"] is default_registry
    assert default_registry.names() == frozenset()
    assert default_registry.schedules() == ()


# --- Real process --------------------------------------------------------------------------


def _start_worker(env_overrides: dict[str, str] | None = None) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TRACEIQ_WORKER_POLL_INTERVAL_SECONDS": "0.2"}
    env.update(env_overrides or {})
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _read_until(process: subprocess.Popen[str], needle: str, timeout: float = 30) -> list[str]:
    assert process.stdout is not None
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        lines.append(line)
        if needle in line:
            return lines
    raise AssertionError(f"{needle!r} not seen; output:\n{''.join(lines)}")


@pytest.mark.db
@pytest.mark.usefixtures("database")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery to a child process")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_process_starts_becomes_ready_and_stops_on_signal(signum: signal.Signals) -> None:
    process = _start_worker()
    try:
        output = _read_until(process, '"message": "Worker ready"')
        process.send_signal(signum)
        remaining, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    output_text = "".join(output) + remaining

    assert process.returncode == 0, output_text
    assert '"message": "Worker shutdown requested"' in output_text
    assert f'"reason": "{signum.name}"' in output_text
    assert '"message": "Worker stopped"' in output_text


@pytest.mark.db
@pytest.mark.usefixtures("database")
def test_process_exits_with_error_when_database_unreachable() -> None:
    process = _start_worker(
        {
            "TRACEIQ_DATABASE_URL": "postgresql+psycopg://traceiq:hunter2-secret@127.0.0.1:1/nowhere_test",
            "TRACEIQ_DATABASE_CONNECT_TIMEOUT": "2",
        }
    )
    try:
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == worker_entrypoint.EXIT_STARTUP_FAILED, output
    assert '"message": "Worker failed to start"' in output
    assert "hunter2-secret" not in output
