"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL, make_url

# Signing secrets must be at least this many bytes; generate one with
# `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
MIN_AUTH_SECRET_BYTES = 32
# A secret with fewer distinct characters than this cannot be 32 random bytes in any common encoding.
_MIN_SECRET_DISTINCT_CHARS = 10
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "replace-me", "replaceme", "example")


class AuthConfigurationError(RuntimeError):
    """Authentication secrets are missing or too weak. The message never contains secret values."""


class Settings(BaseSettings):
    """Runtime settings. Every field maps to a ``TRACEIQ_``-prefixed variable.

    Database connection parts also accept the ``POSTGRES_*`` names used by Docker Compose,
    so the repository-root ``.env`` holds each credential once. ``TRACEIQ_*`` names win.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRACEIQ_",
        # Repository-root .env first, then backend/.env; real environment variables override both.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "TraceIQ"
    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Either set the connection parts below (credentials are used verbatim, no escaping needed),
    # or set a complete URL in database_url, which takes precedence and must already be URL-encoded.
    database_url: str | None = None
    database_driver: str = "postgresql+psycopg"
    database_host: str = Field("localhost", validation_alias=AliasChoices("TRACEIQ_DATABASE_HOST", "POSTGRES_HOST"))
    database_port: int = Field(5433, validation_alias=AliasChoices("TRACEIQ_DATABASE_PORT", "POSTGRES_PORT"))
    database_user: str = Field("traceiq", validation_alias=AliasChoices("TRACEIQ_DATABASE_USER", "POSTGRES_USER"))
    database_password: SecretStr = Field(
        SecretStr(""), validation_alias=AliasChoices("TRACEIQ_DATABASE_PASSWORD", "POSTGRES_PASSWORD")
    )
    database_name: str = Field("traceiq", validation_alias=AliasChoices("TRACEIQ_DATABASE_NAME", "POSTGRES_DB"))
    database_echo: bool = False
    database_connect_timeout: int = 5
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Background jobs (defaults; individual handlers may override lease and max attempts).
    job_default_lease_seconds: PositiveInt = 300
    # Total attempts, including the first one.
    job_default_max_attempts: PositiveInt = 5
    job_backoff_base_seconds: PositiveFloat = 10.0
    job_backoff_max_seconds: PositiveFloat = 3600.0
    worker_poll_interval_seconds: PositiveFloat = 1.0
    worker_maintenance_interval_seconds: PositiveFloat = 15.0

    # Authentication. Secrets are optional here so that processes which do not authenticate (the
    # worker) start without them; the API checks them at startup with validate_auth_secrets().
    # Browser origins allowed for Origin validation are TRACEIQ_CORS_ORIGINS.
    jwt_secret: SecretStr | None = None
    csrf_secret: SecretStr | None = None
    jwt_issuer: str = "traceiq"
    jwt_audience: str = "traceiq-api"
    # Allowed clock skew when validating token timestamps.
    jwt_leeway_seconds: NonNegativeInt = Field(10, le=300)
    access_token_ttl_seconds: PositiveInt = 15 * 60
    # Rolling lifetime of each refresh token; every rotation issues a new one.
    refresh_token_ttl_seconds: PositiveInt = 7 * 24 * 3600
    # Absolute lifetime of a refresh-token family (session), regardless of rotation.
    refresh_token_absolute_ttl_seconds: PositiveInt = 30 * 24 * 3600
    auth_cookie_secure: bool = True
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_cookie_domain: str | None = None
    # None means: enabled in development and test, disabled in production.
    auth_registration_enabled: bool | None = None
    # Process-wide cap on simultaneous Argon2id operations (each uses 64 MiB). Read once per process.
    auth_max_concurrent_hashes: int = Field(4, ge=1, le=64)
    # How long a request waits for a hashing slot before failing with 503 (overload protection).
    auth_hash_wait_timeout_seconds: float = Field(10.0, gt=0, le=120)

    @field_validator("jwt_secret", "csrf_secret", "auth_cookie_domain", "auth_registration_enabled", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("auth_cookie_samesite", mode="before")
    @classmethod
    def _normalize_samesite(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("database_url", mode="before")
    @classmethod
    def _empty_url_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> "Settings":
        if self.job_backoff_max_seconds < self.job_backoff_base_seconds:
            raise ValueError("job_backoff_max_seconds must be greater than or equal to job_backoff_base_seconds")
        return self

    @model_validator(mode="after")
    def _check_auth_settings(self) -> "Settings":
        if self.refresh_token_absolute_ttl_seconds < self.refresh_token_ttl_seconds:
            raise ValueError("refresh_token_absolute_ttl_seconds must be >= refresh_token_ttl_seconds")
        if self.access_token_ttl_seconds > self.refresh_token_ttl_seconds:
            raise ValueError("access_token_ttl_seconds must be <= refresh_token_ttl_seconds")
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise ValueError("auth_cookie_samesite='none' requires auth_cookie_secure=true")
        if self.env == "production" and not self.auth_cookie_secure:
            raise ValueError("auth_cookie_secure must be true in production")
        return self

    @property
    def registration_enabled(self) -> bool:
        if self.auth_registration_enabled is not None:
            return self.auth_registration_enabled
        return self.env != "production"

    def validate_auth_secrets(self) -> None:
        """Raise ``AuthConfigurationError`` unless both signing secrets are present and strong.

        Called by the API at startup. Checks: present, at least ``MIN_AUTH_SECRET_BYTES`` bytes,
        not an obvious placeholder or low-variety value, and the two secrets differ.
        """
        problems: list[str] = []
        values: dict[str, str] = {}
        for name in ("jwt_secret", "csrf_secret"):
            secret: SecretStr | None = getattr(self, name)
            variable = f"TRACEIQ_{name.upper()}"
            if secret is None:
                problems.append(f"{variable} is not set")
                continue
            value = secret.get_secret_value()
            values[name] = value
            if len(value.encode("utf-8")) < MIN_AUTH_SECRET_BYTES:
                problems.append(f"{variable} must be at least {MIN_AUTH_SECRET_BYTES} bytes")
            elif len(set(value)) < _MIN_SECRET_DISTINCT_CHARS or any(
                marker in value.lower() for marker in _PLACEHOLDER_MARKERS
            ):
                problems.append(f"{variable} looks like a placeholder or non-random value")
        if len(values) == 2 and values["jwt_secret"] == values["csrf_secret"]:
            problems.append("TRACEIQ_JWT_SECRET and TRACEIQ_CSRF_SECRET must be different")
        if problems:
            raise AuthConfigurationError(
                "Invalid authentication configuration: "
                + "; ".join(problems)
                + ". Generate each secret with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

    @property
    def sqlalchemy_url(self) -> URL:
        """Database URL as a SQLAlchemy ``URL`` object; never assembled by string formatting."""
        if self.database_url:
            return make_url(self.database_url)
        return URL.create(
            drivername=self.database_driver,
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
