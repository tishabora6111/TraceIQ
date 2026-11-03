"""SQLAlchemy 2.x engine, session factory, and declarative base."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Deterministic constraint names keep Alembic autogenerate diffs stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models. Modules define tables in their ``models.py``."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide engine, created lazily from settings."""
    settings = get_settings()
    return create_engine(
        settings.sqlalchemy_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.database_connect_timeout},
        # Keep SQL parameter values (which may hold payload or user data) out of exception
        # messages, and therefore out of stored job errors and logs. The SQL text is kept.
        hide_parameters=True,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed after the request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database_connection() -> None:
    """Run a trivial query; raises ``sqlalchemy.exc.SQLAlchemyError`` if the database is unreachable."""
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
