"""Run and inspect database migrations without needing alembic.ini."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _base_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def alembic_config(engine: Engine) -> Config:
    config = _base_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config(engine)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_base_config()).get_current_head()


def is_known_revision(revision: str) -> bool:
    """False when the database was migrated by a newer version of the app."""
    try:
        return ScriptDirectory.from_config(_base_config()).get_revision(revision) is not None
    except CommandError:
        return False


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
