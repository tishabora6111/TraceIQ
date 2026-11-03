"""Database error messages never reach log output; other exceptions are logged unchanged."""

import json
import logging
import sys
from collections.abc import Callable, Iterator

import psycopg
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.logging import (
    JsonFormatter,
    SafeStreamHandler,
    configure_logging,
    database_error_metadata,
    find_database_error,
    format_exception_safely,
    is_database_error,
)

# Fake sensitive values standing in for a row PostgreSQL would echo in its DETAIL line.
FAKE_EMAIL = "leak-marker-user@example.com"
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$FAKESALT$FAKEHASHLEAKMARKER"
DRIVER_MESSAGE = (
    'new row for relation "users" violates check constraint "ck_users_email_normalized"\n'
    f"DETAIL:  Failing row contains (0b6c, {FAKE_EMAIL}, {FAKE_HASH}, t)."
)
SENSITIVE = (FAKE_EMAIL, FAKE_HASH, "FAKEHASHLEAKMARKER", "Failing row")


def _driver_error() -> psycopg.Error:
    return psycopg.errors.CheckViolation(DRIVER_MESSAGE)


def _wrapped_error() -> IntegrityError:
    return IntegrityError("INSERT INTO users ...", {"email": FAKE_EMAIL}, _driver_error())


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def _chained() -> BaseException:
    """RuntimeError raised from a wrapped database error, as application code might do."""
    try:
        try:
            raise _wrapped_error()
        except IntegrityError as exc:
            raise RuntimeError("could not save user") from exc
    except RuntimeError as outer:
        return outer


def _format(record_exc: BaseException | None = None, msg: object = "Something failed", *args: object, **extra: object) -> dict:
    exc_info = (type(record_exc), record_exc, record_exc.__traceback__) if record_exc else None
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, msg, args or None, exc_info)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def _assert_no_leak(text: str) -> None:
    for value in SENSITIVE:
        assert value not in text


def test_database_errors_are_recognized() -> None:
    assert is_database_error(_driver_error())
    assert is_database_error(_wrapped_error())
    assert not is_database_error(RuntimeError("x"))
    assert find_database_error(_chained()) is not None
    assert find_database_error(RuntimeError("x")) is None


def test_connection_failures_keep_their_first_line() -> None:
    """Client-side connection errors carry no row data and are needed to diagnose outages."""
    from app.core.logging import describe_database_error

    error = psycopg.OperationalError(
        'connection failed: connection to server at "db" (10.0.0.5), port 5432 failed: Connection refused\n'
        "\tIs the server running on that host and accepting TCP/IP connections?"
    )
    wrapped = IntegrityError("SELECT 1", {}, error)

    for exc in (error, wrapped):
        text = describe_database_error(exc)
        assert text.startswith("<database error message redacted> db_error_type=OperationalError sqlstate=None")
        assert "Connection refused" in text
        assert "Is the server running" not in text  # first line only


def test_connection_failure_urls_are_masked_and_bounded() -> None:
    from app.core.logging import describe_database_error

    text = describe_database_error(psycopg.OperationalError("cannot reach postgresql://app:hunter2@db/x " + "y" * 1000))

    assert "hunter2" not in text
    assert "postgresql://app:***@db/x" in text
    assert len(text) < 500


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.CheckViolation(DRIVER_MESSAGE),  # server error with SQLSTATE
        psycopg.errors.AdminShutdown(f"terminating connection\nDETAIL: {FAKE_EMAIL}"),  # OperationalError + SQLSTATE
        psycopg.DataError(f"bad value {FAKE_EMAIL}"),  # not a connection error
    ],
)
def test_server_and_data_errors_keep_no_message(error: psycopg.Error) -> None:
    from app.core.logging import describe_database_error

    text = describe_database_error(error)

    _assert_no_leak(text)
    assert "connection_error" not in text


def test_metadata_contains_only_safe_fields() -> None:
    metadata = database_error_metadata(_wrapped_error())

    assert metadata == {"db_error_type": "CheckViolation", "sqlstate": "23514", "constraint": None}
    _assert_no_leak(json.dumps(metadata))


@pytest.mark.parametrize("make", [_driver_error, _wrapped_error, _chained])
def test_formatter_redacts_database_errors_in_tracebacks(make) -> None:
    output = _format(_raised(make()))

    _assert_no_leak(json.dumps(output))
    assert "<database error message redacted>" in output["exception"]
    assert "sqlstate=23514" in output["exception"]
    assert "Traceback (most recent call last):" in output["exception"]


def test_formatter_keeps_the_non_database_parts_of_a_chain() -> None:
    output = _format(_chained())["exception"]

    assert "RuntimeError: could not save user" in output
    assert "sqlalchemy.exc.IntegrityError" in output
    assert "The above exception was the direct cause of the following exception:" in output
    # The cause is printed before the exception it caused, as in a standard traceback.
    assert output.index("IntegrityError") < output.index("RuntimeError")


def test_formatter_redacts_database_errors_in_message_arguments_and_extra() -> None:
    output = _format(None, "Save failed: %s", _wrapped_error(), error=_driver_error())

    _assert_no_leak(json.dumps(output))
    assert output["message"].startswith("Save failed: <database error message redacted>")
    assert output["error"].startswith("<database error message redacted>")


def test_formatter_redacts_a_database_error_used_as_the_message() -> None:
    output = _format(None, _wrapped_error())

    _assert_no_leak(json.dumps(output))


def test_other_exceptions_are_formatted_unchanged() -> None:
    try:
        raise ValueError("ordinary failure detail")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", None, exc_info)

    output = json.loads(JsonFormatter().format(record))

    assert output["exception"] == logging.Formatter().formatException(exc_info)
    assert "ValueError: ordinary failure detail" in output["exception"]


def test_uvicorn_error_log_of_a_reraised_database_error_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """Starlette re-raises errors that reach the catch-all handler and uvicorn logs them with a
    traceback; through the configured JSON handler that traceback must be redacted too."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("INFO")
        try:
            raise _chained()
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    finally:
        root.handlers, root.level = saved

    output = capsys.readouterr().out
    assert "Exception in ASGI application" in output
    _assert_no_leak(output)
    assert "sqlstate=23514" in output


class _CurrentStdout:
    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


@pytest.fixture
def json_logging(capsys: pytest.CaptureFixture[str]) -> Iterator[Callable[[], tuple[str, str]]]:
    """Route the "probe" logger to the production handler only, and return captured (stdout, stderr).

    The handler is the one configure_logging() builds. The probe logger does not propagate, so
    pytest's own capture handlers (standard formatter) never see these records; stderr then holds
    only what the logging module's error handler would print.
    """
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    configure_logging("DEBUG")
    [handler] = root.handlers
    root.handlers, root.level = saved
    # pytest swaps sys.stdout between fixture setup and the test body; write to the current one.
    handler.setStream(_CurrentStdout())  # type: ignore[attr-defined]
    probe = logging.getLogger("probe")
    probe.handlers, probe.propagate, probe.level = [handler], False, logging.DEBUG
    try:
        yield lambda: tuple(capsys.readouterr())  # type: ignore[return-value]
    finally:
        probe.handlers, probe.propagate, probe.level = [], True, logging.NOTSET


def _assert_streams_clean(captured: tuple[str, str]) -> None:
    for stream in captured:
        _assert_no_leak(stream)


# --- M-A: exception groups -------------------------------------------------------------------


def _group(*members: BaseException) -> ExceptionGroup:
    return ExceptionGroup("task group failed", list(members))  # type: ignore[arg-type]


def test_database_error_inside_a_group_is_found() -> None:
    error = _wrapped_error()

    assert find_database_error(_group(ValueError("other"), error)) is error


def test_database_error_inside_nested_groups_is_found() -> None:
    error = _wrapped_error()

    assert find_database_error(_group(ValueError("a"), _group(KeyError("b"), _group(error)))) is error


def test_database_error_chained_inside_a_group_member_is_found() -> None:
    assert find_database_error(_group(_chained())) is not None


def test_group_chained_from_a_database_error_is_found() -> None:
    try:
        try:
            raise _wrapped_error()
        except IntegrityError as exc:
            raise _group(ValueError("cleanup failed")) from exc
    except ExceptionGroup as group:
        assert find_database_error(group) is not None


def test_group_without_database_errors_is_not_flagged() -> None:
    assert find_database_error(_group(ValueError("a"), _group(KeyError("b")))) is None


def test_group_cycles_terminate() -> None:
    inner = _group(ValueError("inner"))
    outer = _group(inner)
    inner.__context__ = outer  # cycle through the context link

    assert find_database_error(outer) is None
    assert "ValueError: inner" in format_exception_safely(outer)


@pytest.mark.parametrize(
    "make",
    [
        lambda: _group(_raised(_wrapped_error())),
        lambda: _group(ValueError("other"), _group(_raised(_driver_error()))),
        lambda: _group(_chained()),
    ],
    ids=["group", "nested-group", "chained-member"],
)
def test_groups_are_rendered_without_database_messages(make: Callable[[], BaseException]) -> None:
    group = _raised(make())

    text = format_exception_safely(group)

    _assert_no_leak(text)
    assert "ExceptionGroup: task group failed" in text
    assert "<database error message redacted>" in text
    assert "sqlstate=23514" in text
    assert "+---------------- 1 ----------------" in text


def test_group_members_keep_their_non_database_messages() -> None:
    text = format_exception_safely(_raised(_group(ValueError("visible detail"), _raised(_wrapped_error()))))

    assert "ValueError: visible detail" in text
    assert "+---------------- 2 ----------------" in text


def test_logged_group_is_redacted_on_stdout_and_stderr(json_logging: Callable[[], tuple[str, str]]) -> None:
    try:
        raise _group(ValueError("other"), _group(_raised(_wrapped_error())))
    except ExceptionGroup:
        logging.getLogger("probe").exception("Unhandled")

    captured = json_logging()
    _assert_streams_clean(captured)
    assert "<database error message redacted>" in captured[0]


# --- M-B: the formatter fails closed ---------------------------------------------------------


def test_mismatched_format_arguments_fail_closed(json_logging: Callable[[], tuple[str, str]]) -> None:
    logging.getLogger("probe").error("failed %d", _wrapped_error())

    stdout, stderr = json_logging()
    _assert_streams_clean((stdout, stderr))
    assert stderr == ""  # the logging module's error handler never ran
    record = json.loads(stdout)
    assert set(record) == {"timestamp", "level", "logger", "message", "formatting_error"}
    assert (record["level"], record["logger"], record["message"]) == ("ERROR", "probe", "<log formatting failed>")
    assert record["formatting_error"] == "TypeError"


class _BrokenStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str() exploded")


def test_exception_whose_str_raises_is_rendered_safely(json_logging: Callable[[], tuple[str, str]]) -> None:
    try:
        try:
            raise _wrapped_error()
        except IntegrityError as exc:
            raise _BrokenStr() from exc
    except _BrokenStr:
        logging.getLogger("probe").exception("Unhandled")

    stdout, stderr = json_logging()
    _assert_streams_clean((stdout, stderr))
    assert stderr == ""
    record = json.loads(stdout)
    assert record["message"] == "Unhandled"
    assert "_BrokenStr: <exception str() failed>" in record["exception"]
    assert "<database error message redacted>" in record["exception"]


def test_broken_str_without_database_error_does_not_break_logging(json_logging: Callable[[], tuple[str, str]]) -> None:
    logging.getLogger("probe").error("value: %s", _BrokenStr())

    stdout, stderr = json_logging()
    assert stderr == ""
    assert json.loads(stdout)["message"] == "<log formatting failed>"


def test_unserializable_extra_fails_closed(json_logging: Callable[[], tuple[str, str]]) -> None:
    logging.getLogger("probe").error("x", extra={"payload": _BrokenStr(), "email": FAKE_EMAIL})

    stdout, stderr = json_logging()
    _assert_streams_clean((stdout, stderr))
    record = json.loads(stdout)
    assert record["message"] == "<log formatting failed>"
    assert "email" not in record and "payload" not in record


def test_fallback_contains_only_safe_fields() -> None:
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, f"{FAKE_EMAIL} %d", ("not a number",), None)
    record.secret_field = FAKE_HASH

    output = JsonFormatter().format(record)

    _assert_no_leak(output)
    assert json.loads(output) == {
        "timestamp": json.loads(output)["timestamp"],
        "level": "WARNING",
        "logger": "probe",
        "message": "<log formatting failed>",
        "formatting_error": "TypeError",
    }


def test_safe_exception_message_guards_str() -> None:
    assert format_exception_safely(_raised(_BrokenStr())).endswith("_BrokenStr: <exception str() failed>")


# --- L-a: nested extra fields -----------------------------------------------------------------


def test_nested_extra_values_are_redacted() -> None:
    nested = {
        "list": [_wrapped_error()],
        "tuple": (1, (_driver_error(),)),
        "set": {_driver_error()},
        "dict": {"inner": {"deeper": [{"error": _wrapped_error()}]}},
    }

    output = _format(None, "Something failed", nested=nested)

    _assert_no_leak(json.dumps(output))
    assert output["nested"]["list"][0].startswith("<database error message redacted>")
    assert output["nested"]["dict"]["inner"]["deeper"][0]["error"].startswith("<database error message redacted>")


def test_nested_message_arguments_are_redacted() -> None:
    output = _format(None, "Errors: %s", [_wrapped_error(), {"cause": _driver_error()}])

    _assert_no_leak(json.dumps(output))
    assert output["message"].count("<database error message redacted>") == 2


def test_database_error_dict_keys_are_redacted() -> None:
    output = _format(None, "x", keyed={_driver_error(): "value"})

    _assert_no_leak(json.dumps(output))


def test_cyclic_containers_do_not_recurse_forever() -> None:
    cyclic_list: list[object] = [_wrapped_error()]
    cyclic_list.append(cyclic_list)
    cyclic_dict: dict[str, object] = {"error": _driver_error()}
    cyclic_dict["self"] = cyclic_dict

    output = _format(None, "x", cyclic_list=cyclic_list, cyclic_dict=cyclic_dict)

    _assert_no_leak(json.dumps(output))
    assert output["cyclic_list"][1] == "<cycle>"
    assert output["cyclic_dict"]["self"] == "<cycle>"


def test_shared_non_cyclic_references_are_kept() -> None:
    shared = ["value"]

    output = _format(None, "x", pair=[shared, shared])

    assert output["pair"] == [["value"], ["value"]]


def test_non_database_values_are_unchanged_by_redaction() -> None:
    output = _format(None, "x", data={"count": 3, "items": [1, 2], "flag": True})

    assert output["data"] == {"count": 3, "items": [1, 2], "flag": True}


def test_format_exception_safely_handles_context_and_cycles() -> None:
    try:
        try:
            raise _driver_error()
        except psycopg.Error:
            raise KeyError("during handling")
    except KeyError as exc:
        caught = exc
    caught.__context__.__context__ = caught  # type: ignore[union-attr]  # a cycle must not loop forever

    text = format_exception_safely(caught)

    _assert_no_leak(text)
    assert "During handling of the above exception, another exception occurred:" in text
    assert "KeyError: 'during handling'" in text


# --- A failing log stream never reveals the record --------------------------------------------

STREAM_MARKER = "STREAM-FAILURE-MARKER-7f3c9e"


def _marked_db_error() -> IntegrityError:
    orig = psycopg.errors.CheckViolation(f"violates\nDETAIL:  Failing row contains ({STREAM_MARKER}).")
    return IntegrityError("INSERT INTO users ...", {"email": STREAM_MARKER}, orig)


class _BrokenStream:
    """A log stream whose writes always fail, e.g. a closed pipe."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        raise OSError("stream closed")

    def flush(self) -> None:
        raise OSError("stream closed")


def _safe_logger(stream: object) -> tuple[logging.Logger, SafeStreamHandler]:
    handler = SafeStreamHandler(stream)  # type: ignore[arg-type]
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("stream-probe")
    logger.handlers, logger.propagate, logger.level = [handler], False, logging.DEBUG
    return logger, handler


@pytest.fixture
def broken_stream_logger() -> Iterator[tuple[logging.Logger, _BrokenStream]]:
    stream = _BrokenStream()
    logger, _ = _safe_logger(stream)
    yield logger, stream
    logger.handlers, logger.propagate, logger.level = [], True, logging.NOTSET


def _log_everything_sensitive(logger: logging.Logger) -> None:
    """Exercise every place a record can carry the marker: message, arguments, extra, traceback."""
    error = _marked_db_error()
    logger.error(f"plain {STREAM_MARKER}")
    logger.error("argument %s", STREAM_MARKER)
    logger.error("database error argument %s", error)
    logger.error("extra", extra={"email": STREAM_MARKER, "nested": [error]})
    try:
        raise RuntimeError(STREAM_MARKER) from error
    except RuntimeError:
        logger.exception("Unhandled %s", STREAM_MARKER)


def test_failing_stream_never_reveals_the_record(
    broken_stream_logger: tuple[logging.Logger, _BrokenStream], capsys: pytest.CaptureFixture[str]
) -> None:
    logger, stream = broken_stream_logger

    _log_everything_sensitive(logger)  # must not raise

    stdout, stderr = capsys.readouterr()
    assert STREAM_MARKER not in stdout
    assert STREAM_MARKER not in stderr
    assert "Failing row" not in stderr and "Traceback" not in stderr and "Arguments" not in stderr
    assert stream.writes == 5
    # One fixed notice per failed record, naming only the error class.
    assert stderr.splitlines() == ["--- Logging error: a log record could not be written (OSError); record withheld ---"] * 5


def test_standard_handler_would_reveal_the_record(capsys: pytest.CaptureFixture[str]) -> None:
    """Documents the finding: the stdlib handler prints the raw arguments when the stream fails."""
    handler = logging.StreamHandler(_BrokenStream())  # type: ignore[arg-type]
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("stdlib-probe")
    logger.handlers, logger.propagate = [handler], False
    try:
        logger.error("argument %s", STREAM_MARKER)
    finally:
        logger.handlers, logger.propagate = [], True

    assert STREAM_MARKER in capsys.readouterr().err


def test_failure_notice_does_not_recurse(
    broken_stream_logger: tuple[logging.Logger, _BrokenStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If stderr itself logs (e.g. it is redirected into logging), the handler must not loop."""
    logger, stream = broken_stream_logger
    notices: list[str] = []

    class LoggingStderr:
        def write(self, text: str) -> int:
            notices.append(text)
            logger.error("stderr was written: %s", STREAM_MARKER)  # re-enters the failing handler
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stderr", LoggingStderr())

    logger.error("argument %s", STREAM_MARKER)

    # The outer failure wrote one notice; the re-entrant record failed silently instead of looping.
    assert len(notices) == 1
    assert STREAM_MARKER not in notices[0]
    assert stream.writes == 2


def test_broken_stderr_is_tolerated(
    broken_stream_logger: tuple[logging.Logger, _BrokenStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, stream = broken_stream_logger
    monkeypatch.setattr(sys, "stderr", _BrokenStream())

    logger.error("argument %s", STREAM_MARKER)  # must not raise

    assert stream.writes == 1


def test_missing_stderr_is_tolerated(
    broken_stream_logger: tuple[logging.Logger, _BrokenStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, _ = broken_stream_logger
    monkeypatch.setattr(sys, "stderr", None)

    logger.error("argument %s", STREAM_MARKER)  # must not raise


def test_failure_notice_respects_raise_exceptions(
    broken_stream_logger: tuple[logging.Logger, _BrokenStream],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger, _ = broken_stream_logger
    monkeypatch.setattr(logging, "raiseExceptions", False)

    logger.error("argument %s", STREAM_MARKER)

    assert capsys.readouterr().err == ""


def test_handler_recovers_when_the_stream_works_again(capsys: pytest.CaptureFixture[str]) -> None:
    stream = _BrokenStream()
    logger, handler = _safe_logger(stream)
    try:
        logger.error("first %s", STREAM_MARKER)
        handler.stream = _CurrentStdout()  # setStream() would flush the broken stream first
        logger.info("second record", extra={"request_id": "r-1"})
    finally:
        logger.handlers, logger.propagate, logger.level = [], True, logging.NOTSET

    stdout, stderr = capsys.readouterr()
    assert STREAM_MARKER not in stdout + stderr
    record = json.loads(stdout)
    assert (record["message"], record["request_id"], record["level"]) == ("second record", "r-1", "INFO")
    assert stderr.count("record withheld") == 1


def test_working_stream_logs_normally(capsys: pytest.CaptureFixture[str]) -> None:
    logger, _ = _safe_logger(_CurrentStdout())
    try:
        logger.warning("hello %s", "world", extra={"user_id": "u-1"})
        try:
            raise ValueError("ordinary failure")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.handlers, logger.propagate, logger.level = [], True, logging.NOTSET

    stdout, stderr = capsys.readouterr()
    assert stderr == ""
    first, second = (json.loads(line) for line in stdout.splitlines())
    assert (first["message"], first["user_id"], first["level"]) == ("hello world", "u-1", "WARNING")
    assert second["message"] == "failed"
    assert "ValueError: ordinary failure" in second["exception"]


def test_configure_logging_installs_the_safe_handler() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("INFO")
        [handler] = root.handlers
        assert isinstance(handler, SafeStreamHandler)
        assert isinstance(handler.formatter, JsonFormatter)
    finally:
        root.handlers, root.level = saved
