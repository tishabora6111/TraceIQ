import uuid
from collections.abc import Iterator

import pytest
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, get_settings
from app.core.database import check_database_connection, get_db, get_engine
from app.modules.jobs.execution import format_error

RESERVED_PASSWORD = "p@ss:w/rd#50%?&=+ $x"

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("database")]


def test_database_connection_succeeds() -> None:
    check_database_connection()


def test_session_executes_query_against_postgresql() -> None:
    session_gen = get_db()
    session = next(session_gen)
    try:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert "PostgreSQL" in session.execute(text("SELECT version()")).scalar_one()
    finally:
        session_gen.close()


def _run_admin_sql(statement: sql.Composed) -> None:
    # Runs on the psycopg connection directly so identifiers and the password literal are
    # quoted by psycopg; the commit must therefore also happen on the driver connection.
    with get_engine().connect() as connection:
        driver_connection = connection.connection.driver_connection
        assert driver_connection is not None
        driver_connection.execute(statement)
        driver_connection.commit()


def _role_exists(role: str) -> bool:
    with get_engine().connect() as connection:
        return connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).first() is not None


@pytest.fixture
def reserved_password_role() -> Iterator[str]:
    """A temporary login role whose password contains URL-reserved characters."""
    role = f"traceiq_test_{uuid.uuid4().hex[:12]}"
    try:
        _run_admin_sql(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(RESERVED_PASSWORD))
        )
    except pg_errors.InsufficientPrivilege:
        pytest.skip("Configured database user cannot create roles")
    try:
        assert _role_exists(role), "test role was not created"
        yield role
    finally:
        _run_admin_sql(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        assert not _role_exists(role), "test role was not dropped"


def _settings_for(role: str, password: str) -> Settings:
    base = get_settings().sqlalchemy_url
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=None,
        database_driver=base.drivername,
        database_host=base.host,
        database_port=base.port,
        database_name=base.database,
        database_user=role,
        database_password=password,
        database_connect_timeout=get_settings().database_connect_timeout,
    )


def test_connects_with_password_containing_url_reserved_characters(reserved_password_role: str) -> None:
    settings = _settings_for(reserved_password_role, RESERVED_PASSWORD)
    engine = create_engine(settings.sqlalchemy_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == reserved_password_role
    finally:
        engine.dispose()


def test_wrong_password_is_rejected(reserved_password_role: str) -> None:
    """Guards against a false positive: the server really checks the password."""
    settings = _settings_for(reserved_password_role, RESERVED_PASSWORD + "-wrong")
    engine = create_engine(settings.sqlalchemy_url, connect_args={"connect_timeout": 5})
    try:
        with pytest.raises(OperationalError, match="password authentication failed"):
            with engine.connect():
                pass
    finally:
        engine.dispose()


# --- SQL parameter values in errors ----------------------------------------------------------

SENSITIVE_VALUE = "patient-ssn-123-45-6789"


def _database_error_with_sensitive_parameter() -> Exception:
    """Raise a real database error through the app's engine for a statement with a sensitive parameter.

    Division by zero is used because PostgreSQL's own message for it does not echo any value.
    """
    session_gen = get_db()
    session = next(session_gen)
    try:
        session.execute(text("SELECT :value WHERE 1 / 0 = 1"), {"value": SENSITIVE_VALUE})
    except Exception as exc:
        return exc
    finally:
        session_gen.close()
    raise AssertionError("statement unexpectedly succeeded")


def test_engine_hides_sql_parameters() -> None:
    assert get_engine().hide_parameters is True


def test_database_error_does_not_contain_parameter_values() -> None:
    exc = _database_error_with_sensitive_parameter()

    assert SENSITIVE_VALUE not in str(exc)
    assert "SQL parameters hidden" in str(exc)


def test_formatted_database_error_hides_parameters_but_keeps_diagnostics() -> None:
    message = format_error(_database_error_with_sensitive_parameter())

    assert SENSITIVE_VALUE not in message
    # Database messages can embed row values, so only safe diagnostics remain: the exception
    # type, the driver error class and the SQLSTATE.
    assert message == (
        "DataError: <database error message redacted> db_error_type=DivisionByZero sqlstate=22012 constraint=None"
    )
