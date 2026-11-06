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

from app.core.database import Base
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.jobs import models as _jobs_models  # noqa: F401
from app.modules.projects import models as _projects_models  # noqa: F401
from tests.conftest import ADMIN_URL, alembic_config, create_database, drop_database

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("database")]

BASELINE = "0001_baseline"
JOBS = "0002_jobs"
AUTH = "0003_auth"
PROJECTS = "0004_projects"
HEAD = PROJECTS
JOB_TABLES = {"jobs", "job_schedules"}
AUTH_TABLES = {"users", "refresh_tokens"}
PROJECT_TABLES = {"projects", "project_members", "api_keys"}


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
    assert _current_revision(migration_engine) == HEAD
    assert JOB_TABLES | AUTH_TABLES | PROJECT_TABLES <= _tables(migration_engine)

    # Autogenerate finds no difference between the models and the migrated schema.
    _check(migration_engine)


def test_auth_downgrade_and_repeated_upgrade(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    assert _current_revision(migration_engine) == HEAD

    # Downgrading auth (and the projects migration that depends on it) leaves the jobs tables.
    _downgrade(migration_engine, JOBS)
    assert _current_revision(migration_engine) == JOBS
    assert _tables(migration_engine) == {"alembic_version"} | JOB_TABLES

    _upgrade(migration_engine, AUTH)
    assert _tables(migration_engine) == {"alembic_version"} | JOB_TABLES | AUTH_TABLES

    # Upgrading an up-to-date database is a no-op.
    _upgrade(migration_engine, "head")
    _upgrade(migration_engine, "head")
    assert _current_revision(migration_engine) == HEAD
    _check(migration_engine)


def test_projects_downgrade_and_repeated_upgrade(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    assert _current_revision(migration_engine) == PROJECTS

    # Downgrading projects removes only its tables; auth and jobs stay.
    _downgrade(migration_engine, AUTH)
    assert _current_revision(migration_engine) == AUTH
    assert _tables(migration_engine) == {"alembic_version"} | JOB_TABLES | AUTH_TABLES
    with migration_engine.connect() as connection:
        leftover = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE indexname LIKE ANY (ARRAY['%project%', '%api_key%'])")
        ).all()
    assert leftover == []

    _upgrade(migration_engine, PROJECTS)
    assert _tables(migration_engine) == {"alembic_version"} | JOB_TABLES | AUTH_TABLES | PROJECT_TABLES
    _upgrade(migration_engine, "head")  # no-op
    assert _current_revision(migration_engine) == PROJECTS
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


# --- Auth schema -----------------------------------------------------------------------------


def test_users_schema(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("users")}

    assert set(columns) == {
        "id", "email", "password_hash", "is_active", "created_at", "updated_at", "last_login_at",
    }  # fmt: skip
    assert {name for name, column in columns.items() if column["nullable"]} == {"last_login_at"}
    assert columns["id"]["default"] is None
    assert columns["email"]["type"].length == 320
    for timestamp in ("created_at", "updated_at", "last_login_at"):
        assert columns[timestamp]["type"].timezone is True

    assert inspector.get_pk_constraint("users")["name"] == "pk_users"
    assert {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints("users")} == {
        ("uq_users_email", ("email",))
    }
    assert {c["name"] for c in inspector.get_check_constraints("users")} == {
        "ck_users_email_normalized",
        "ck_users_email_has_at",
        "ck_users_password_hash_not_empty",
    }


def test_refresh_tokens_schema(migration_engine: Engine) -> None:
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("refresh_tokens")}

    assert set(columns) == {
        "id", "user_id", "family_id", "token_hash", "expires_at", "absolute_expires_at",
        "revoked_at", "revocation_reason", "replaced_by_id", "created_at",
    }  # fmt: skip
    assert {name for name, column in columns.items() if column["nullable"]} == {
        "revoked_at", "revocation_reason", "replaced_by_id",
    }  # fmt: skip
    assert columns["id"]["default"] is None
    for timestamp in ("expires_at", "absolute_expires_at", "revoked_at", "created_at"):
        assert columns[timestamp]["type"].timezone is True

    foreign_keys = {
        fk["name"]: (fk["constrained_columns"], fk["referred_table"], fk["options"].get("ondelete"))
        for fk in inspector.get_foreign_keys("refresh_tokens")
    }
    assert foreign_keys == {
        "fk_refresh_tokens_user_id_users": (["user_id"], "users", "CASCADE"),
        "fk_refresh_tokens_replaced_by_id_refresh_tokens": (["replaced_by_id"], "refresh_tokens", "SET NULL"),
    }
    assert {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints("refresh_tokens")} == {
        ("uq_refresh_tokens_token_hash", ("token_hash",)),
        ("uq_refresh_tokens_replaced_by_id", ("replaced_by_id",)),
    }
    assert {c["name"] for c in inspector.get_check_constraints("refresh_tokens")} == {
        "ck_refresh_tokens_token_hash_format",
        "ck_refresh_tokens_expiry_within_absolute",
        "ck_refresh_tokens_revocation_reason",
        "ck_refresh_tokens_revoked_iff_reason",
        "ck_refresh_tokens_replaced_only_when_rotated",
        "ck_refresh_tokens_not_replaced_by_self",
    }


def test_auth_indexes(migration_engine: Engine) -> None:
    with migration_engine.connect() as connection:
        index_defs = dict(
            connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN ('users', 'refresh_tokens')")
            ).all()
        )

    assert set(index_defs) == {
        "pk_users",
        "uq_users_email",
        "pk_refresh_tokens",
        "uq_refresh_tokens_token_hash",
        "uq_refresh_tokens_replaced_by_id",
        "ix_refresh_tokens_user_id",
        "ix_refresh_tokens_family_id",
    }
    assert index_defs["uq_users_email"].startswith("CREATE UNIQUE INDEX")
    assert index_defs["uq_refresh_tokens_token_hash"].startswith("CREATE UNIQUE INDEX")
    assert "(user_id)" in index_defs["ix_refresh_tokens_user_id"]
    assert "(family_id)" in index_defs["ix_refresh_tokens_family_id"]


# --- Models and migrations agree --------------------------------------------------------------

_CONSTRAINT_DEFS = text(
    "SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)"
    " FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid"
    " JOIN pg_namespace ns ON ns.oid = rel.relnamespace"
    " WHERE ns.nspname = 'public' AND rel.relname <> 'alembic_version'"
)
_INDEX_DEFS = text("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")


def _schema_definitions(engine: Engine) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    with engine.connect() as connection:
        constraints = {tuple(row) for row in connection.execute(_CONSTRAINT_DEFS)}
        indexes = {tuple(row) for row in connection.execute(_INDEX_DEFS) if row[0] != "alembic_version"}
    return constraints, indexes


def test_migrated_constraints_and_indexes_match_the_models(migration_engine: Engine) -> None:
    """`alembic check` ignores check-constraint bodies; compare the full definitions instead."""
    _upgrade(migration_engine, "head")
    name = f"{ADMIN_URL.database}_metadata_test"
    engine = create_engine(create_database(name, recreate=True), connect_args={"connect_timeout": 5})
    try:
        Base.metadata.create_all(engine)
        from_models = _schema_definitions(engine)
    finally:
        engine.dispose()
        drop_database(name)

    from_migrations = _schema_definitions(migration_engine)
    assert from_migrations[0] == from_models[0]
    assert from_migrations[1] == from_models[1]
    assert any(table == "refresh_tokens" for table, *_ in from_models[0])


def test_migrated_revocation_reasons(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    with migration_engine.connect() as connection:
        definition = connection.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_refresh_tokens_revocation_reason'")
        ).scalar_one()

    for reason in ("rotated", "reuse_detected", "logout", "revoked"):
        assert f"'{reason}'" in definition
    assert definition.count("::character varying") == 4  # exactly four allowed values


# --- Projects schema -----------------------------------------------------------------------


def _foreign_keys(inspector, table: str) -> dict[str, tuple[list[str], str, str | None]]:  # type: ignore[no-untyped-def]
    return {
        fk["name"]: (fk["constrained_columns"], fk["referred_table"], fk["options"].get("ondelete"))
        for fk in inspector.get_foreign_keys(table)
    }


def test_projects_schema(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("projects")}

    assert set(columns) == {"id", "name", "description", "retention_overrides", "created_at", "updated_at"}
    assert {name for name, column in columns.items() if column["nullable"]} == {"description", "retention_overrides"}
    assert columns["id"]["default"] is None
    assert (columns["name"]["type"].length, columns["description"]["type"].length) == (100, 1000)
    assert type(columns["retention_overrides"]["type"]).__name__ == "JSONB"
    assert columns["created_at"]["type"].timezone and columns["updated_at"]["type"].timezone
    assert {c["name"] for c in inspector.get_check_constraints("projects")} == {
        "ck_projects_name_trimmed",
        "ck_projects_retention_overrides_object",
    }


def test_project_members_schema(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("project_members")}

    assert set(columns) == {"project_id", "user_id", "role", "created_at", "updated_at"}
    assert not any(column["nullable"] for column in columns.values())
    primary_key = inspector.get_pk_constraint("project_members")
    assert (primary_key["name"], primary_key["constrained_columns"]) == ("pk_project_members", ["project_id", "user_id"])
    assert _foreign_keys(inspector, "project_members") == {
        "fk_project_members_project_id_projects": (["project_id"], "projects", "CASCADE"),
        "fk_project_members_user_id_users": (["user_id"], "users", "RESTRICT"),
    }
    assert {c["name"] for c in inspector.get_check_constraints("project_members")} == {"ck_project_members_role"}


def test_api_keys_schema(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    inspector = inspect(migration_engine)
    columns = {column["name"]: column for column in inspector.get_columns("api_keys")}

    assert set(columns) == {
        "id", "project_id", "name", "prefix", "key_hash", "created_by",
        "created_at", "last_used_at", "revoked_at", "revoked_by",
    }  # fmt: skip
    assert {name for name, column in columns.items() if column["nullable"]} == {
        "created_by", "last_used_at", "revoked_at", "revoked_by",
    }  # fmt: skip
    assert columns["id"]["default"] is None
    assert (columns["name"]["type"].length, columns["prefix"]["type"].length, columns["key_hash"]["type"].length) == (
        100,
        16,
        64,
    )
    for timestamp in ("created_at", "last_used_at", "revoked_at"):
        assert columns[timestamp]["type"].timezone is True
    assert _foreign_keys(inspector, "api_keys") == {
        "fk_api_keys_project_id_projects": (["project_id"], "projects", "CASCADE"),
        "fk_api_keys_created_by_users": (["created_by"], "users", "SET NULL"),
        "fk_api_keys_revoked_by_users": (["revoked_by"], "users", "SET NULL"),
    }
    assert {c["name"] for c in inspector.get_check_constraints("api_keys")} == {
        "ck_api_keys_key_hash_format",
        "ck_api_keys_revoked_by_requires_revoked_at",
        "ck_api_keys_name_trimmed",
        "ck_api_keys_prefix_not_empty",
    }
    assert {(c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints("api_keys")} == {
        ("uq_api_keys_key_hash", ("key_hash",))
    }


def test_projects_indexes(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    with migration_engine.connect() as connection:
        index_defs = dict(
            connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes"
                    " WHERE tablename IN ('projects', 'project_members', 'api_keys')"
                )
            ).all()
        )

    assert set(index_defs) == {
        "pk_projects",
        "pk_project_members",
        "ix_project_members_user_id",
        "uq_project_members_owner",
        "pk_api_keys",
        "uq_api_keys_key_hash",
        "ix_api_keys_project_id",
        "ix_api_keys_created_by",
        "ix_api_keys_revoked_by",
    }
    owner = index_defs["uq_project_members_owner"]
    assert owner.startswith("CREATE UNIQUE INDEX") and "(project_id)" in owner
    assert "WHERE ((role)::text = 'owner'::text)" in owner
    assert index_defs["uq_api_keys_key_hash"].startswith("CREATE UNIQUE INDEX")


def test_project_member_roles_are_exactly_the_approved_values(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "head")
    with migration_engine.connect() as connection:
        definition = connection.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_project_members_role'")
        ).scalar_one()

    for role in ("owner", "admin", "member"):
        assert f"'{role}'" in definition
    assert definition.count("::character varying") == 3
