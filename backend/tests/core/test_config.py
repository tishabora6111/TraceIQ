from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from app.core.config import AuthConfigurationError, Settings

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


JOB_ENV_VARS = (
    "TRACEIQ_JOB_DEFAULT_LEASE_SECONDS",
    "TRACEIQ_JOB_DEFAULT_MAX_ATTEMPTS",
    "TRACEIQ_JOB_BACKOFF_BASE_SECONDS",
    "TRACEIQ_JOB_BACKOFF_MAX_SECONDS",
    "TRACEIQ_WORKER_POLL_INTERVAL_SECONDS",
    "TRACEIQ_WORKER_MAINTENANCE_INTERVAL_SECONDS",
)


@pytest.fixture
def _no_job_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in JOB_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("_no_job_env")
def test_job_settings_defaults() -> None:
    settings = make_settings()

    assert settings.job_default_lease_seconds == 300
    assert settings.job_default_max_attempts == 5
    assert settings.job_backoff_base_seconds == 10
    assert settings.job_backoff_max_seconds == 3600
    assert settings.worker_poll_interval_seconds == 1
    assert settings.worker_maintenance_interval_seconds == 15


@pytest.mark.usefixtures("_no_job_env")
def test_job_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_JOB_DEFAULT_LEASE_SECONDS", "120")
    monkeypatch.setenv("TRACEIQ_JOB_DEFAULT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("TRACEIQ_JOB_BACKOFF_BASE_SECONDS", "2.5")
    monkeypatch.setenv("TRACEIQ_JOB_BACKOFF_MAX_SECONDS", "60")
    monkeypatch.setenv("TRACEIQ_WORKER_POLL_INTERVAL_SECONDS", "0.25")
    monkeypatch.setenv("TRACEIQ_WORKER_MAINTENANCE_INTERVAL_SECONDS", "5")

    settings = make_settings()

    assert settings.job_default_lease_seconds == 120
    assert settings.job_default_max_attempts == 3
    assert settings.job_backoff_base_seconds == 2.5
    assert settings.job_backoff_max_seconds == 60
    assert settings.worker_poll_interval_seconds == 0.25
    assert settings.worker_maintenance_interval_seconds == 5


@pytest.mark.usefixtures("_no_job_env")
@pytest.mark.parametrize(
    "field",
    [
        "job_default_lease_seconds",
        "job_default_max_attempts",
        "job_backoff_base_seconds",
        "job_backoff_max_seconds",
        "worker_poll_interval_seconds",
        "worker_maintenance_interval_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_job_settings_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: value})


@pytest.mark.usefixtures("_no_job_env")
def test_backoff_max_must_not_be_below_base() -> None:
    with pytest.raises(ValidationError, match="job_backoff_max_seconds"):
        make_settings(job_backoff_base_seconds=30, job_backoff_max_seconds=10)


# --- Authentication settings -----------------------------------------------------------------

AUTH_ENV_VARS = (
    "TRACEIQ_JWT_SECRET",
    "TRACEIQ_CSRF_SECRET",
    "TRACEIQ_JWT_ISSUER",
    "TRACEIQ_JWT_AUDIENCE",
    "TRACEIQ_JWT_LEEWAY_SECONDS",
    "TRACEIQ_ACCESS_TOKEN_TTL_SECONDS",
    "TRACEIQ_REFRESH_TOKEN_TTL_SECONDS",
    "TRACEIQ_REFRESH_TOKEN_ABSOLUTE_TTL_SECONDS",
    "TRACEIQ_AUTH_COOKIE_SECURE",
    "TRACEIQ_AUTH_COOKIE_SAMESITE",
    "TRACEIQ_AUTH_COOKIE_DOMAIN",
    "TRACEIQ_AUTH_REGISTRATION_ENABLED",
    "TRACEIQ_ENV",
)
STRONG_JWT_SECRET = "Zq3vN8wL1xK7pR2tY6uB9mC4hJ0sD5fG-jwt"
STRONG_CSRF_SECRET = "Hk2mP9qW4zR7tX1vB6nL3cF8yJ5sD0gA-csrf"


@pytest.fixture
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("_no_auth_env")
def test_auth_settings_defaults() -> None:
    settings = make_settings()

    assert settings.jwt_secret is None and settings.csrf_secret is None
    assert (settings.jwt_issuer, settings.jwt_audience, settings.jwt_leeway_seconds) == ("traceiq", "traceiq-api", 10)
    assert settings.access_token_ttl_seconds == 15 * 60
    assert settings.refresh_token_ttl_seconds == 7 * 24 * 3600
    assert settings.refresh_token_absolute_ttl_seconds == 30 * 24 * 3600
    assert settings.auth_cookie_secure is True
    assert settings.auth_cookie_samesite == "lax"
    assert settings.auth_cookie_domain is None


@pytest.mark.usefixtures("_no_auth_env")
def test_settings_load_without_auth_secrets() -> None:
    # Processes that do not authenticate (the worker) must start without secrets.
    settings = make_settings(env="production")

    assert settings.jwt_secret is None
    assert settings.sqlalchemy_url is not None


@pytest.mark.usefixtures("_no_auth_env")
def test_auth_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_JWT_SECRET", STRONG_JWT_SECRET)
    monkeypatch.setenv("TRACEIQ_CSRF_SECRET", STRONG_CSRF_SECRET)
    monkeypatch.setenv("TRACEIQ_ACCESS_TOKEN_TTL_SECONDS", "300")
    monkeypatch.setenv("TRACEIQ_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("TRACEIQ_AUTH_COOKIE_SAMESITE", "Strict")
    monkeypatch.setenv("TRACEIQ_AUTH_COOKIE_DOMAIN", "example.com")

    settings = make_settings()

    assert settings.jwt_secret is not None and settings.jwt_secret.get_secret_value() == STRONG_JWT_SECRET
    assert settings.csrf_secret is not None and settings.csrf_secret.get_secret_value() == STRONG_CSRF_SECRET
    assert settings.access_token_ttl_seconds == 300
    assert settings.auth_cookie_secure is False
    assert settings.auth_cookie_samesite == "strict"
    assert settings.auth_cookie_domain == "example.com"


@pytest.mark.usefixtures("_no_auth_env")
def test_empty_secrets_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_JWT_SECRET", "")
    monkeypatch.setenv("TRACEIQ_CSRF_SECRET", "")

    settings = make_settings()
    assert settings.jwt_secret is None and settings.csrf_secret is None


@pytest.mark.usefixtures("_no_auth_env")
def test_secrets_are_hidden_in_repr() -> None:
    settings = make_settings(jwt_secret=STRONG_JWT_SECRET, csrf_secret=STRONG_CSRF_SECRET)

    assert STRONG_JWT_SECRET not in repr(settings) and STRONG_CSRF_SECRET not in repr(settings)
    assert STRONG_JWT_SECRET not in str(settings.model_dump())


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize(
    ("env", "override", "expected"),
    [
        ("development", None, True),
        ("test", None, True),
        ("production", None, False),
        ("production", True, True),
        ("development", False, False),
    ],
)
def test_registration_enabled(env: str, override: bool | None, expected: bool) -> None:
    assert make_settings(env=env, auth_registration_enabled=override).registration_enabled is expected


@pytest.mark.usefixtures("_no_auth_env")
def test_valid_auth_secrets_pass() -> None:
    make_settings(jwt_secret=STRONG_JWT_SECRET, csrf_secret=STRONG_CSRF_SECRET).validate_auth_secrets()


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize(
    ("jwt_secret", "csrf_secret", "expected"),
    [
        (None, None, ["TRACEIQ_JWT_SECRET is not set", "TRACEIQ_CSRF_SECRET is not set"]),
        (None, STRONG_CSRF_SECRET, ["TRACEIQ_JWT_SECRET is not set"]),
        (STRONG_JWT_SECRET, None, ["TRACEIQ_CSRF_SECRET is not set"]),
        ("short-secret", STRONG_CSRF_SECRET, ["TRACEIQ_JWT_SECRET must be at least 32 bytes"]),
        (STRONG_JWT_SECRET, "x" * 31, ["TRACEIQ_CSRF_SECRET must be at least 32 bytes"]),
        ("a" * 64, STRONG_CSRF_SECRET, ["TRACEIQ_JWT_SECRET looks like a placeholder or non-random value"]),
        ("ab" * 16, STRONG_CSRF_SECRET, ["TRACEIQ_JWT_SECRET looks like a placeholder"]),
        ("change-me-change-me-change-me-1234567", STRONG_CSRF_SECRET, ["TRACEIQ_JWT_SECRET looks like a placeholder"]),
        (STRONG_JWT_SECRET, "replace-me-with-a-real-secret-0123456", ["TRACEIQ_CSRF_SECRET looks like a placeholder"]),
        (STRONG_JWT_SECRET, STRONG_JWT_SECRET, ["must be different"]),
    ],
)
def test_invalid_auth_secrets_are_rejected(
    jwt_secret: str | None, csrf_secret: str | None, expected: list[str]
) -> None:
    settings = make_settings(jwt_secret=jwt_secret, csrf_secret=csrf_secret)

    with pytest.raises(AuthConfigurationError) as exc_info:
        settings.validate_auth_secrets()
    message = str(exc_info.value)
    for fragment in expected:
        assert fragment in message
    assert "secrets.token_urlsafe" in message  # tells the operator how to fix it
    for secret in (jwt_secret, csrf_secret):
        if secret and len(secret) >= 8:
            assert secret not in message


@pytest.mark.usefixtures("_no_auth_env")
def test_secret_length_is_measured_in_bytes() -> None:
    # 16 two-byte characters = 32 bytes.
    make_settings(jwt_secret="éàüöäçñøåæœßþðłž", csrf_secret=STRONG_CSRF_SECRET).validate_auth_secrets()


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"refresh_token_ttl_seconds": 3600, "refresh_token_absolute_ttl_seconds": 600}, "refresh_token_absolute_ttl"),
        ({"access_token_ttl_seconds": 7200, "refresh_token_ttl_seconds": 3600}, "access_token_ttl_seconds"),
        ({"auth_cookie_samesite": "none", "auth_cookie_secure": False}, "requires auth_cookie_secure"),
        ({"auth_cookie_samesite": "loose"}, "auth_cookie_samesite"),
        ({"access_token_ttl_seconds": 0}, "access_token_ttl_seconds"),
        ({"jwt_leeway_seconds": -1}, "jwt_leeway_seconds"),
        ({"jwt_leeway_seconds": 301}, "jwt_leeway_seconds"),
    ],
)
def test_invalid_auth_settings_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)


@pytest.mark.usefixtures("_no_auth_env")
def test_samesite_none_allowed_with_secure_cookies() -> None:
    assert make_settings(auth_cookie_samesite="NONE", auth_cookie_secure=True).auth_cookie_samesite == "none"


@pytest.mark.usefixtures("_no_auth_env")
def test_secure_cookies_are_required_in_production() -> None:
    with pytest.raises(ValidationError, match="auth_cookie_secure must be true in production"):
        make_settings(env="production", auth_cookie_secure=False)


@pytest.mark.usefixtures("_no_auth_env")
def test_secure_cookies_from_the_environment_are_checked_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_ENV", "production")
    monkeypatch.setenv("TRACEIQ_AUTH_COOKIE_SECURE", "false")

    with pytest.raises(ValidationError, match="auth_cookie_secure must be true in production"):
        make_settings()


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize("env", ["development", "test"])
def test_insecure_cookies_are_allowed_outside_production(env: str) -> None:
    assert make_settings(env=env, auth_cookie_secure=False).auth_cookie_secure is False


@pytest.mark.usefixtures("_no_auth_env")
def test_production_accepts_secure_cookies() -> None:
    assert make_settings(env="production").auth_cookie_secure is True


@pytest.mark.usefixtures("_no_auth_env")
def test_max_concurrent_hashes_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings().auth_max_concurrent_hashes == 4
    monkeypatch.setenv("TRACEIQ_AUTH_MAX_CONCURRENT_HASHES", "8")
    assert make_settings().auth_max_concurrent_hashes == 8
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            make_settings(auth_max_concurrent_hashes=invalid)


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize("limit", [1, 64])
def test_max_concurrent_hashes_accepts_the_bounds(limit: int) -> None:
    assert make_settings(auth_max_concurrent_hashes=limit).auth_max_concurrent_hashes == limit


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize("limit", [0, 65, 10_000])
def test_max_concurrent_hashes_rejects_out_of_range_values(limit: int) -> None:
    with pytest.raises(ValidationError, match="auth_max_concurrent_hashes"):
        make_settings(auth_max_concurrent_hashes=limit)


@pytest.mark.usefixtures("_no_auth_env")
@pytest.mark.parametrize(("env", "enabled"), [("development", True), ("production", False)])
def test_empty_registration_override_means_environment_default(
    monkeypatch: pytest.MonkeyPatch, env: str, enabled: bool
) -> None:
    """Docker Compose passes unset variables as empty strings."""
    monkeypatch.setenv("TRACEIQ_AUTH_REGISTRATION_ENABLED", "")

    settings = make_settings(env=env)

    assert settings.auth_registration_enabled is None
    assert settings.registration_enabled is enabled


@pytest.mark.usefixtures("_no_auth_env")
def test_hash_wait_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings().auth_hash_wait_timeout_seconds == 10.0
    monkeypatch.setenv("TRACEIQ_AUTH_HASH_WAIT_TIMEOUT_SECONDS", "2.5")
    assert make_settings().auth_hash_wait_timeout_seconds == 2.5
    for invalid in (0, -1, 121):
        with pytest.raises(ValidationError, match="auth_hash_wait_timeout_seconds"):
            make_settings(auth_hash_wait_timeout_seconds=invalid)


@pytest.mark.usefixtures("_no_auth_env")
def test_max_concurrent_hashes_out_of_range_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEIQ_AUTH_MAX_CONCURRENT_HASHES", "65")

    with pytest.raises(ValidationError, match="auth_max_concurrent_hashes"):
        make_settings()
