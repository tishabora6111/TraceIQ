"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL, make_url


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
