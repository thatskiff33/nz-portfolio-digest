"""Alembic environment. Reads the database URL from settings, never from alembic.ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy.types import TypeDecorator

from portfolio_digest.config import get_settings
from portfolio_digest.db import make_engine
from portfolio_digest.models import Base

target_metadata = Base.metadata

# Only the `alembic` command line has an ini file; the app configures its own output.
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    return get_settings().database_url.get_secret_value()


def _render_item(type_, obj, autogen_context):
    # Write our guard types (e.g. ExactNumeric) as the plain column type they store, so
    # migrations never depend on app code that may change later.
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl_instance!r}"
    return False


CONFIGURE_OPTS = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
    "render_item": _render_item,
}


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, **CONFIGURE_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A connection passed in by the CLI or tests takes priority over settings.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(_url())
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    context.configure(connection=connection, **CONFIGURE_OPTS)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
