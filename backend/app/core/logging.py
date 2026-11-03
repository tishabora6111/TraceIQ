"""Structured (JSON) logging using the standard library."""

import json
import logging
import sys
import threading
import traceback
from datetime import UTC, datetime
from typing import Any

import psycopg
from sqlalchemy.exc import DBAPIError

# Attributes present on every LogRecord; anything else was passed via ``extra=`` and is emitted as a field.
_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName", "color_message"}
)


def is_database_error(exc: BaseException) -> bool:
    """Database driver errors, raw or wrapped by SQLAlchemy.

    Their messages can embed row contents (PostgreSQL's ``DETAIL: Failing row contains (...)``),
    which ``hide_parameters`` does not hide, so they must never be logged verbatim.
    """
    return isinstance(exc, DBAPIError | psycopg.Error)


def _related_exceptions(exc: BaseException) -> list[BaseException]:
    """Exceptions linked to ``exc``: its cause, its context, and exception-group members."""
    related = [linked for linked in (exc.__cause__, exc.__context__) if linked is not None]
    if isinstance(exc, BaseExceptionGroup):
        related.extend(exc.exceptions)
    return related


def find_database_error(exc: BaseException) -> BaseException | None:
    """The first database error reachable from ``exc`` through causes, contexts and exception
    groups (at any depth), if any. Cycles are visited once."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if is_database_error(current):
            return current
        pending.extend(reversed(_related_exceptions(current)))
    return None


def database_error_metadata(exc: BaseException) -> dict[str, str | None]:
    """Safe fields describing a database error: driver error class, SQLSTATE, constraint name."""
    driver_error = exc.orig if isinstance(exc, DBAPIError) else exc
    diag = getattr(driver_error, "diag", None)
    return {
        "db_error_type": type(driver_error).__name__,
        "sqlstate": getattr(driver_error, "sqlstate", None),
        "constraint": getattr(diag, "constraint_name", None),
    }


_STR_FAILED = "<exception str() failed>"


def _safe_exception_message(exc: BaseException) -> str:
    if not is_database_error(exc):
        try:
            return str(exc)
        except Exception:
            return _STR_FAILED
    fields = database_error_metadata(exc)
    return "<database error message redacted> " + " ".join(f"{key}={value}" for key, value in fields.items())


_CAUSE_LINK = "The above exception was the direct cause of the following exception:"
_CONTEXT_LINK = "During handling of the above exception, another exception occurred:"


def format_exception_safely(exc: BaseException) -> str:
    """Like ``traceback.format_exception``, but database error messages are replaced by safe metadata.

    Exception-group members are rendered, indented, below their group. Each exception is rendered
    at most once, so cycles terminate.
    """
    return "\n".join(_format_chain(exc, set()))


def _format_chain(exc: BaseException, seen: set[int]) -> list[str]:
    chain: list[tuple[BaseException, str | None]] = []
    current: BaseException | None = exc
    link: str | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, link))
        if current.__cause__ is not None:
            current, link = current.__cause__, _CAUSE_LINK
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, _CONTEXT_LINK
        else:
            current = None

    parts: list[str] = []
    for item, following_link in reversed(chain):
        lines = traceback.format_tb(item.__traceback__)
        if lines:
            parts.append("Traceback (most recent call last):\n" + "".join(lines))
        name = type(item).__qualname__
        module = type(item).__module__
        qualified = name if module in ("builtins", "__main__") else f"{module}.{name}"
        message = _safe_exception_message(item)
        parts.append(f"{qualified}: {message}" if message else qualified)
        if isinstance(item, BaseExceptionGroup):
            for index, member in enumerate(item.exceptions, start=1):
                member_lines = _format_chain(member, seen)
                if member_lines:
                    parts.append(f"+---------------- {index} ----------------")
                    parts.extend("    " + line for line in "\n".join(member_lines).splitlines())
        if following_link:
            parts.append(f"\n{following_link}\n")
    return parts


_CYCLE = "<cycle>"


def _redact(value: Any, _path: frozenset[int] = frozenset()) -> Any:
    """``value`` with database errors replaced by safe metadata, inside dicts, lists, tuples and
    sets at any depth. A container that contains itself is replaced by ``"<cycle>"`` where it recurs."""
    if isinstance(value, BaseException):
        return _safe_exception_message(value) if is_database_error(value) else value
    if not isinstance(value, dict | list | tuple | set | frozenset):
        return value
    if id(value) in _path:
        return _CYCLE
    path = _path | {id(value)}
    if isinstance(value, dict):
        return {_redact(key, path): _redact(item, path) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, path) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, path) for item in value)
    return {_redact(item, path) for item in value}


def _safe_message(record: logging.LogRecord) -> str:
    """``record.getMessage()`` with any database error in the message or its arguments redacted."""
    msg = _redact(record.msg)
    args = _redact(record.args) if isinstance(record.args, tuple | dict) else record.args
    message = str(msg)
    return message % args if args else message


_FORMAT_FAILED = "<log formatting failed>"


def _fallback_record(record: logging.LogRecord, error: BaseException) -> str:
    """The fail-closed log line: only fields that cannot carry the original message or values."""
    try:
        timestamp = datetime.fromtimestamp(float(record.created), tz=UTC).isoformat()
    except Exception:
        timestamp = None
    fields = {
        "timestamp": timestamp,
        "level": record.levelname if isinstance(record.levelname, str) else None,
        "logger": record.name if isinstance(record.name, str) else None,
        "message": _FORMAT_FAILED,
        "formatting_error": type(error).__name__,
    }
    return json.dumps(fields)


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects. Database error messages are redacted.

    Never raises: if a record cannot be formatted, a minimal fallback line is returned instead,
    because the logging module's own error handler would print the raw message and arguments.
    """

    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging API name
        exc = ei[1]
        if isinstance(exc, BaseException) and find_database_error(exc) is not None:
            return format_exception_safely(exc)
        return super().formatException(ei)

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format(record)
        except Exception as error:
            return _fallback_record(record, error)

    def _format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_message(record),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = _redact(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_EMIT_FAILED_NOTICE = "--- Logging error: a log record could not be written ({error}); record withheld ---\n"


class SafeStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A stream handler whose failure report never contains the record.

    The standard ``handleError`` prints the failing record's message, arguments and the current
    traceback to stderr, which can include unredacted database errors. This one writes a fixed
    notice naming only the error class, never logs (so it cannot recurse into itself), and never
    raises: a broken log stream must not break the application.
    """

    _handling = threading.local()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API name
        if not logging.raiseExceptions or getattr(self._handling, "active", False):
            return
        self._handling.active = True
        try:
            stderr = sys.stderr
            if stderr is not None:
                error = sys.exc_info()[1]
                stderr.write(_EMIT_FAILED_NOTICE.format(error=type(error).__name__ if error else "unknown"))
                stderr.flush()
        except Exception:  # stderr is broken too: stay silent
            pass
        finally:
            self._handling.active = False


def configure_logging(level: str = "INFO") -> None:
    """Route root and uvicorn loggers through a single JSON handler on stdout."""
    handler = SafeStreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
