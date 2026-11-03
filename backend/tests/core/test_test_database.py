"""Guards for the test-database isolation set up in tests/conftest.py."""

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.core.database import get_engine
from tests.conftest import (
    ADMIN_URL,
    TEST_DATABASE_SUFFIX,
    alembic_config,
    assert_test_database,
    truncate_application_tables,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("database")]


def test_settings_point_at_test_database() -> None:
    assert (get_settings().sqlalchemy_url.database or "").endswith(TEST_DATABASE_SUFFIX)


def test_engine_is_connected_to_test_database() -> None:
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT current_database()")).scalar_one().endswith(TEST_DATABASE_SUFFIX)


def test_test_database_is_migrated_to_head() -> None:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()

    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head


def test_truncate_refuses_non_test_database() -> None:
    if (ADMIN_URL.database or "").endswith(TEST_DATABASE_SUFFIX):
        pytest.skip("Configured database is itself a _test database")
    engine = create_engine(ADMIN_URL, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="Refusing to modify database"):
                assert_test_database(connection)
            with pytest.raises(RuntimeError, match="Refusing to modify database"):
                truncate_application_tables(connection)
    finally:
        engine.dispose()


def test_clean_database_fixture_runs(clean_database: None) -> None:
    with get_engine().connect() as connection:
        assert_test_database(connection)
