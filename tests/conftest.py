"""Test fixtures. Database tests need a throwaway Postgres in TEST_DATABASE_URL.

Every test run wipes and rebuilds the `public` schema of that database, so never point it
at real data. As a safety net, the tests refuse to run unless the database name contains
"test". CI sets REQUIRE_TEST_DB=1 so a missing database fails instead of skipping.
"""

import os

import pytest
from sqlalchemy import Engine, make_url, text

from portfolio_digest import migrate
from portfolio_digest.config import get_settings
from portfolio_digest.db import make_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _reset_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        if os.environ.get("REQUIRE_TEST_DB") == "1":
            pytest.fail("TEST_DATABASE_URL is not set but REQUIRE_TEST_DB=1.")
        pytest.skip("TEST_DATABASE_URL not set; skipping database tests.")
    if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
        pytest.fail("TEST_DATABASE_URL must name a database containing 'test'; it gets wiped.")
    return TEST_DATABASE_URL


@pytest.fixture
def engine(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A freshly migrated, empty database. The CLI sees it through DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    eng = make_engine(database_url)
    _reset_schema(eng)
    migrate.upgrade(eng)
    yield eng
    eng.dispose()
    get_settings.cache_clear()


@pytest.fixture
def empty_engine(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A database with no tables at all."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    eng = make_engine(database_url)
    _reset_schema(eng)
    yield eng
    eng.dispose()
    get_settings.cache_clear()


@pytest.fixture
def no_settings_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings come only from the environment the test sets, never a stray `.env`."""
    monkeypatch.chdir(tmp_path)
    for name in ("DATABASE_URL", "BASE_CURRENCY", "TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
