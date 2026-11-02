"""Alembic migration environment.

The database connection comes from application settings, never from alembic.ini. Online
migrations reuse the application's engine (built from a SQLAlchemy ``URL`` object), so
credentials with URL-reserved characters need no escaping.
Module ORM models must be imported below so autogenerate can see their tables.
"""

from logging.config import fileConfig

from alembic import context

from app.core.config import get_settings
from app.core.database import Base, get_engine

# Import module models here as they are added, e.g.:
# from app.modules.projects import models as _projects_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection."""
    context.configure(
        url=get_settings().sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
