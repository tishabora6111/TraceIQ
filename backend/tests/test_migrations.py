"""Migration tests, run against a dedicated, freshly created database.

The shared test database is left alone; this module creates ``<name>_migrations_test``,
walks the migration history, and drops it afterwards.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ADMIN_URL, alembic_config, create_database, drop_database

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("database")]

BASELINE = "0001_baseline"
JOBS = "0002_jobs"
JOB_TABLES = {"jobs", "job_schedules"}


@pytest.fixture(scope="module")
def migration_engine() -> Iterator[Engine]:
    name = f"{ADMIN_URL.database}_migrations_test"
    url = create_database(name, recreate=True)
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        yield engine
    finally:
        engine.dispose()
        drop_database(name)


def _upgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), revision)


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _check(engine: Engine) -> None:
    with engine.connect() as connection:
        command.check(alembic_config(connection))


def test_upgrade_downgrade_upgrade_cycle(migration_engine: Engine) -> None:
    assert _current_revision(migration_engine) is None

    _upgrade(migration_engine, BASELINE)
    assert _current_revision(migration_engine) == BASELINE
    assert _tables(migration_engine) == {"alembic_version"}

    _upgrade(migration_engine, JOBS)
    assert _current_revision(migration_engine) == JOBS
    assert JOB_TABLES <= _tables(migration_engine)

    _downgrade(migration_engine, BASELINE)
    assert _current_revision(migration_engine) == BASELINE
    assert _tables(migration_engine) == {"alembic_version"}
    # Sanity check that `alembic check` detects drift: the models define tables the database lacks.
    with pytest.raises(CommandError, match="not up to date"):
        _check(migration_engine)

    _upgrade(migration_engine, "head")
    assert _current_revision(migration_engine) == JOBS
    assert JOB_TABLES <= _tables(migration_engine)

    # Autogenerate finds no difference between the models and the migrated schema.
    _check(migration_engine)


def test_alembic_check_on_shared_test_database() -> None:
    command.check(alembic_config())


def test_jobs_columns(migration_engine: Engine) -> None:
    columns = {column["name"]: column for column in inspect(migration_engine).get_columns("jobs")}

    assert set(columns) == {
        "id", "type", "status", "payload", "attempts", "max_attempts", "run_at",
        "locked_by", "locked_at", "lease_expires_at", "started_at", "finished_at",
        "last_error", "dedupe_key", "created_at", "updated_at",
    }  # fmt: skip
    nullable = {name for name, column in columns.items() if column["nullable"]}
    assert nullable == {
        "locked_by", "locked_at", "lease_expires_at", "started_at", "finished_at", "last_error", "dedupe_key",
    }  # fmt: skip
    # UUIDs are generated in Python: no database default on the primary key.
    assert columns["id"]["default"] is None
    for timestamp in ("run_at", "locked_at", "lease_expires_at", "started_at", "finished_at", "created_at"):
        assert columns[timestamp]["type"].timezone is True


def test_jobs_constraints_and_indexes(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)

    checks = {constraint["name"] for constraint in inspector.get_check_constraints("jobs")}
    assert checks == {
        "ck_jobs_status",
        "ck_jobs_attempts_non_negative",
        "ck_jobs_max_attempts_positive",
        "ck_jobs_attempts_within_max",
        "ck_jobs_running_is_locked",
        "ck_jobs_not_running_is_unlocked",
        "ck_jobs_finished_iff_terminal",
    }

    with migration_engine.connect() as connection:
        index_defs = dict(
            connection.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'")).all()
        )
    assert set(index_defs) == {
        "pk_jobs",
        "ix_jobs_queued_run_at",
        "ix_jobs_running_lease_expires_at",
        "ix_jobs_type_status",
        "ix_jobs_finished_at",
        "uq_jobs_active_dedupe_key",
    }
    assert "WHERE ((status)::text = 'queued'::text)" in index_defs["ix_jobs_queued_run_at"]
    assert "WHERE ((status)::text = 'running'::text)" in index_defs["ix_jobs_running_lease_expires_at"]
    assert index_defs["uq_jobs_active_dedupe_key"].startswith("CREATE UNIQUE INDEX")
    assert "dedupe_key IS NOT NULL" in index_defs["uq_jobs_active_dedupe_key"]


def test_job_schedules_schema(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("job_schedules")}

    assert set(columns) == {
        "name", "job_type", "interval_seconds", "payload", "enabled",
        "next_run_at", "last_enqueued_at", "created_at", "updated_at",
    }  # fmt: skip
    assert inspector.get_pk_constraint("job_schedules")["constrained_columns"] == ["name"]
    assert {c["name"] for c in inspector.get_check_constraints("job_schedules")} == {
        "ck_job_schedules_interval_positive"
    }
    assert {i["name"] for i in inspector.get_indexes("job_schedules")} == {"ix_job_schedules_enabled_next_run_at"}


# --- Constraint behaviour --------------------------------------------------------------------

_INSERT = text(
    "INSERT INTO jobs (id, type, status, max_attempts, attempts, locked_by, locked_at, lease_expires_at,"
    " started_at, finished_at, dedupe_key)"
    " VALUES (:id, 'test.job', :status, :max_attempts, :attempts, :locked_by, :locked_at, :lease,"
    " :started_at, :finished_at, :dedupe_key)"
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "status": "queued",
        "max_attempts": 5,
        "attempts": 0,
        "locked_by": None,
        "locked_at": None,
        "lease": None,
        "started_at": None,
        "finished_at": None,
        "dedupe_key": None,
    }
    row.update(overrides)
    return row


def _insert(engine: Engine, *rows: dict[str, object]) -> None:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs"))
        for row in rows:
            connection.execute(_INSERT, row)


@pytest.mark.parametrize(
    ("label", "row"),
    [
        ("unknown status", _row(status="paused")),
        ("negative attempts", _row(attempts=-1)),
        ("zero max_attempts", _row(max_attempts=0, attempts=0)),
        ("attempts above max", _row(attempts=6, max_attempts=5)),
        ("running without lock", _row(status="running", attempts=1)),
        ("queued with lock", _row(locked_by="w1")),
        ("succeeded without finished_at", _row(status="succeeded", attempts=1)),
        ("queued with finished_at", _row(finished_at="2026-11-03T00:00:00Z")),
    ],
)
def test_invalid_job_rows_are_rejected(migration_engine: Engine, label: str, row: dict[str, object]) -> None:
    with pytest.raises(IntegrityError, match="ck_jobs_"):
        _insert(migration_engine, row)


def test_valid_job_rows_are_accepted(migration_engine: Engine) -> None:
    ts = "2026-11-03T00:00:00Z"
    _insert(
        migration_engine,
        _row(),
        _row(status="running", attempts=1, locked_by="w1", locked_at=ts, lease=ts, started_at=ts),
        _row(status="succeeded", attempts=1, started_at=ts, finished_at=ts),
        _row(status="failed", attempts=5, started_at=ts, finished_at=ts),
    )


def test_dedupe_key_unique_only_among_active_jobs(migration_engine: Engine) -> None:
    ts = "2026-11-03T00:00:00Z"
    finished = {"status": "succeeded", "attempts": 1, "started_at": ts, "finished_at": ts}

    # Finished jobs do not block a new active job with the same key.
    _insert(migration_engine, _row(dedupe_key="k", **finished), _row(dedupe_key="k", **finished), _row(dedupe_key="k"))

    # Two active jobs with the same key are rejected.
    with pytest.raises(IntegrityError, match="uq_jobs_active_dedupe_key"):
        _insert(migration_engine, _row(dedupe_key="k"), _row(dedupe_key="k"))

    # Jobs without a key are unaffected.
    _insert(migration_engine, _row(), _row())


def test_schedule_interval_must_be_positive(migration_engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_job_schedules_interval_positive"):
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job_schedules (name, job_type, interval_seconds, next_run_at)"
                    " VALUES ('s', 'test.job', 0, now())"
                )
            )
