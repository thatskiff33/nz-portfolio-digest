"""Alembic environment. Reads the database URL from settings, never from alembic.ini."""

from alembic import context

from portfolio_digest.config import get_settings
from portfolio_digest.db import make_engine
from portfolio_digest.models import Base

target_metadata = Base.metadata


def _url() -> str:
    # A connection passed in by the CLI or tests takes priority over settings.
    configured = context.config.attributes.get("database_url")
    return configured or get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(_url())
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
