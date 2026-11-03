"""Shared test fixtures.

Database isolation: every test that touches PostgreSQL runs against a separate database named
``<configured name>_test`` (or the configured name itself if it already ends in ``_test``). The
redirect happens at import time below, before any engine is created, so tests can never reach the
development database. The ``database`` fixture creates the test database if needed and migrates it
to the Alembic head; ``clean_database`` truncates application tables after verifying, against the
live connection, that it is connected to a ``_test`` database.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import URL, Connection, text

from app.core.config import Settings, get_settings
from app.core.database import Base, get_engine, get_session_factory

TEST_DATABASE_SUFFIX = "_test"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Fixed signing secrets for the test suite only (the API refuses to start without valid ones).
# Set before any settings are loaded; real environment variables take precedence over .env.
TEST_JWT_SECRET = "test-jwt-secret-Vq8mZ2xR5tN1wK7pL4bC9hJ3sD6fG0yA"
TEST_CSRF_SECRET = "test-csrf-secret-Hn4rT8vB2kM6qW1zX5cJ9pL3sF7dG0yE"
os.environ["TRACEIQ_JWT_SECRET"] = TEST_JWT_SECRET
os.environ["TRACEIQ_CSRF_SECRET"] = TEST_CSRF_SECRET


def _redirect_to_test_database() -> URL:
    """Point all app settings at the test database; return the original (admin) URL."""
    configured = get_settings().sqlalchemy_url
    name = configured.database or ""
    test_name = name if name.endswith(TEST_DATABASE_SUFFIX) else f"{name}{TEST_DATABASE_SUFFIX}"
    os.environ["TRACEIQ_DATABASE_URL"] = configured.set(database=test_name).render_as_string(hide_password=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return configured


ADMIN_URL = _redirect_to_test_database()


def assert_test_database(connection: Connection) -> None:
    """Refuse to continue unless the live connection is to a ``_test`` database."""
    current = connection.execute(text("SELECT current_database()")).scalar_one()
    if not current.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(f"Refusing to modify database {current!r}: not a {TEST_DATABASE_SUFFIX} database")


def truncate_application_tables(connection: Connection) -> None:
    assert_test_database(connection)
    tables = [table.name for table in reversed(Base.metadata.sorted_tables)]
    if tables:
        connection.execute(
            text("TRUNCATE " + ", ".join(connection.dialect.identifier_preparer.quote(t) for t in tables) + " CASCADE")
        )


def alembic_config(connection: Connection | None = None) -> Config:
    """Alembic config for programmatic use; optionally bound to an open connection."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    config.attributes["configure_logger"] = False
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _unavailable(message: str) -> None:
    if os.environ.get("TRACEIQ_TEST_REQUIRE_DB") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _admin_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=ADMIN_URL.host,
        port=ADMIN_URL.port,
        dbname=ADMIN_URL.database,
        user=ADMIN_URL.username,
        password=ADMIN_URL.password,
        connect_timeout=get_settings().database_connect_timeout,
        autocommit=True,
    )


def create_database(name: str, *, recreate: bool = False) -> URL:
    """Create a ``_test`` database (dropping it first if ``recreate``). Returns its URL."""
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(f"Refusing to create or drop database {name!r}: not a {TEST_DATABASE_SUFFIX} database")
    try:
        with _admin_connection() as admin:
            if recreate:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if not exists:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        _unavailable(f"Test database {name!r} cannot be created by the configured user")
    except psycopg.OperationalError as exc:
        _unavailable(f"PostgreSQL not reachable with the configured database settings: {exc}")
    return ADMIN_URL.set(database=name)


def drop_database(name: str) -> None:
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(f"Refusing to drop database {name!r}: not a {TEST_DATABASE_SUFFIX} database")
    with _admin_connection() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database() -> str:
    """Ensure the test database exists and is migrated to head. Returns its name."""
    test_name = get_settings().sqlalchemy_url.database
    assert test_name and test_name.endswith(TEST_DATABASE_SUFFIX)
    create_database(test_name)
    command.upgrade(alembic_config(), "head")
    return test_name


@pytest.fixture
def clean_database(database: str) -> Iterator[None]:
    """Start the test with empty application tables."""
    with get_engine().begin() as connection:
        truncate_application_tables(connection)
    yield


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"env": "test"})


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    # HTTPS, so Secure cookies behave as they do in production.
    with TestClient(create_app(settings), base_url="https://testserver", raise_server_exceptions=False) as test_client:
        yield test_client
