"""Run and inspect database migrations without needing alembic.ini."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    """Alembic settings for our migrations. Commands run on `connection` when given."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def upgrade(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")


def head_revision() -> str | None:
    return _scripts().get_current_head()


def is_known_revision(revision: str) -> bool:
    """False when the database was migrated by a newer version of the app."""
    try:
        return _scripts().get_revision(revision) is not None
    except CommandError:
        return False


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
