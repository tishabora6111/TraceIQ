"""Background worker entry point: ``python -m app.worker``.

Runs in its own process, never inside the API. Exit codes: 0 after a graceful shutdown,
1 if the worker could not start, 130 if forced to stop by a second signal.
"""

import logging
import sys

from app.core.config import get_settings
from app.core.database import get_engine
from app.core.logging import configure_logging
from app.modules.jobs.registry import default_registry
from app.modules.jobs.runner import Worker, WorkerStartupError

# Job handlers and schedules are registered on ``default_registry`` by importing each module's
# ``jobs.py`` explicitly here (never discovered dynamically). None exist yet in M1.2, e.g.:
# from app.modules.incidents import jobs as _incidents_jobs  # noqa: F401

logger = logging.getLogger("app.worker")

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_INTERRUPTED = 130


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker(registry=default_registry, settings=settings)
    logger.info("Worker starting", extra={"worker_id": worker.worker_id, "environment": settings.env})
    try:
        with worker.handle_signals():
            worker.run()
    except WorkerStartupError as exc:
        logger.error("Worker failed to start", extra={"worker_id": worker.worker_id, "error": str(exc)})
        return EXIT_STARTUP_FAILED
    except KeyboardInterrupt:
        logger.warning("Worker forced to stop", extra={"worker_id": worker.worker_id})
        return EXIT_INTERRUPTED
    finally:
        get_engine().dispose()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
