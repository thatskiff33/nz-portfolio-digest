"""Database engine and session helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from portfolio_digest.config import ConfigError, get_settings


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url.get_secret_value()
    try:
        parsed = make_url(url)
        if parsed.get_backend_name() == "postgresql":
            return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        return create_engine(url, pool_pre_ping=True)
    except (ArgumentError, ValueError, ImportError):
        # The original message can quote the URL, password and all. ImportError means a
        # driver we do not ship, such as mysql://.
        raise ConfigError("DATABASE_URL is not a valid database URL. See .env.example.") from None


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """A session that commits on success and rolls back on any error."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
