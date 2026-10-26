import pytest

from portfolio_digest.config import ConfigError, get_settings
from portfolio_digest.db import make_engine

URL = "postgresql+psycopg://portfolio:s3cret-pass@db:5432/portfolio"


def test_missing_database_url_names_the_setting(no_settings_file):
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        get_settings()


def test_settings_are_read_from_a_dotenv_file(no_settings_file, tmp_path):
    (tmp_path / ".env").write_text(f"DATABASE_URL={URL}\nBASE_CURRENCY=aud\n")
    settings = get_settings()
    assert settings.database_url.get_secret_value() == URL
    assert settings.base_currency == "AUD"


@pytest.mark.parametrize(
    ("name", "value"), [("BASE_CURRENCY", "dollars"), ("TIMEZONE", "Middle/Earth")]
)
def test_invalid_settings_are_reported_without_their_value(
    no_settings_file, monkeypatch, name, value
):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name) as exc:
        get_settings()
    assert value not in str(exc.value)
    assert "s3cret-pass" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "s3cret-pass",
        "postgresql+psycopg://portfolio:s3cret-pass@db:notaport/portfolio",
        "nosuchdriver://portfolio:s3cret-pass@db/portfolio",
    ],
)
def test_a_malformed_database_url_is_never_echoed(url):
    with pytest.raises(ConfigError) as exc:
        make_engine(url)
    assert "s3cret-pass" not in str(exc.value)
    assert exc.value.__suppress_context__
