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
    ("name", "value"),
    [
        ("BASE_CURRENCY", "dollars"),
        ("TIMEZONE", "Middle/Earth"),
        ("PRICE_SOURCE", "paid-but-not-installed"),
        ("FX_SOURCES", "yahoo,unknown"),
        ("FX_SOURCES", ""),
    ],
)
def test_invalid_settings_are_reported_without_their_value(
    no_settings_file, monkeypatch, name, value
):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name) as exc:
        get_settings()
    assert not value or value not in str(exc.value)
    assert "s3cret-pass" not in str(exc.value)


def test_market_data_source_names_are_normalised(no_settings_file, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PRICE_SOURCE", " YAHOO ")
    monkeypatch.setenv("FX_SOURCES", " FRANKFURTER, YAHOO ")
    settings = get_settings()
    assert settings.price_source == "yahoo"
    assert settings.fx_source_names == ["frankfurter", "yahoo"]


@pytest.mark.parametrize(
    "url",
    [
        "s3cret-pass",
        "postgresql+psycopg://portfolio:s3cret-pass@db:notaport/portfolio",
        "nosuchdriver://portfolio:s3cret-pass@db/portfolio",
        "mysql://portfolio:s3cret-pass@db/portfolio",
    ],
)
def test_a_malformed_database_url_is_never_echoed(url):
    with pytest.raises(ConfigError) as exc:
        make_engine(url)
    assert "s3cret-pass" not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("dotenv", [False, True])
def test_report_settings_are_independent_and_mask_secrets(
    no_settings_file, monkeypatch, tmp_path, dotenv
):
    from portfolio_digest.config import get_report_settings

    values = {
        "REPORT_RECIPIENTS": "one@example.invalid,two@example.invalid",
        "REPORT_KUMA_PUSH_URL": "https://monitor.example.invalid/invented-token",
    }
    if dotenv:
        (tmp_path / ".env").write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    else:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    settings = get_report_settings()
    assert settings.recipients == values["REPORT_RECIPIENTS"]
    assert settings.kuma_push_url.get_secret_value() == values["REPORT_KUMA_PUSH_URL"]
    assert "invented-token" not in repr(settings)
