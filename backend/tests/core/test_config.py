from pathlib import Path

import pytest
from sqlalchemy import make_url

from app.core.config import Settings

RESERVED_PASSWORD = "p@ss:w/rd#50%?&=+ $x"

DB_ENV_VARS = (
    "TRACEIQ_DATABASE_URL",
    "TRACEIQ_DATABASE_HOST",
    "TRACEIQ_DATABASE_PORT",
    "TRACEIQ_DATABASE_USER",
    "TRACEIQ_DATABASE_PASSWORD",
    "TRACEIQ_DATABASE_NAME",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DB_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_url_is_built_from_parts_with_reserved_password() -> None:
    url = make_settings(
        database_host="db", database_port=5432, database_user="trace:iq", database_password=RESERVED_PASSWORD
    ).sqlalchemy_url

    assert url.password == RESERVED_PASSWORD
    assert url.username == "trace:iq"
    assert url.host == "db"
    assert url.port == 5432
    assert url.database == "traceiq"


def test_rendered_url_escapes_password_and_round_trips() -> None:
    url = make_settings(database_password=RESERVED_PASSWORD).sqlalchemy_url

    rendered = url.render_as_string(hide_password=False)
    assert RESERVED_PASSWORD not in rendered
    assert make_url(rendered).password == RESERVED_PASSWORD


def test_password_is_hidden_in_default_rendering_and_repr() -> None:
    settings = make_settings(database_password=RESERVED_PASSWORD)

    assert RESERVED_PASSWORD not in str(settings.sqlalchemy_url)
    assert RESERVED_PASSWORD not in repr(settings)


def test_parts_read_from_traceiq_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_DATABASE_HOST", "db")
    monkeypatch.setenv("TRACEIQ_DATABASE_PORT", "5432")
    monkeypatch.setenv("TRACEIQ_DATABASE_PASSWORD", RESERVED_PASSWORD)

    url = make_settings().sqlalchemy_url

    assert (url.host, url.port, url.password) == ("db", 5432, RESERVED_PASSWORD)


def test_parts_fall_back_to_postgres_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "pguser")
    monkeypatch.setenv("POSTGRES_PASSWORD", RESERVED_PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "pgdb")
    monkeypatch.setenv("POSTGRES_PORT", "6543")

    url = make_settings().sqlalchemy_url

    assert (url.username, url.password, url.database, url.port) == ("pguser", RESERVED_PASSWORD, "pgdb", 6543)


def test_traceiq_variables_take_precedence_over_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-postgres")
    monkeypatch.setenv("TRACEIQ_DATABASE_PASSWORD", RESERVED_PASSWORD)

    assert make_settings().sqlalchemy_url.password == RESERVED_PASSWORD


def test_parts_read_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"POSTGRES_PASSWORD='{RESERVED_PASSWORD}'\n", encoding="utf-8")

    url = Settings(_env_file=env_file).sqlalchemy_url  # type: ignore[call-arg]

    assert url.password == RESERVED_PASSWORD


def test_explicit_url_overrides_parts() -> None:
    settings = make_settings(
        database_url="postgresql+psycopg://u:p%40ss@h:1/d",
        database_password="ignored",
    )

    url = settings.sqlalchemy_url
    assert (url.username, url.password, url.host, url.port, url.database) == ("u", "p@ss", "h", 1, "d")


def test_empty_url_falls_back_to_parts() -> None:
    assert make_settings(database_url="", database_host="db").sqlalchemy_url.host == "db"
