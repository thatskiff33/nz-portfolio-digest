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


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("yes", True), ("true", True), ("0", False), ("no", False), ("false", False)],
)
def test_dashboard_boolean_settings(no_settings_file, monkeypatch, value, expected):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIE", value)
    settings = get_settings()
    assert settings.dashboard_secure_cookie is expected
    assert settings.dashboard_password is None
    assert settings.dashboard_bind == "127.0.0.1"


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_dashboard_settings_and_password_masking(no_settings_file, monkeypatch, tmp_path, source):
    values = {
        "DATABASE_URL": URL,
        "DASHBOARD_PASSWORD": "fictional-dashboard-secret",
        "DASHBOARD_BIND": "100.100.20.30",
        "DASHBOARD_SECURE_COOKIE": "yes",
        "DASHBOARD_HOSTNAMES": "Machine.tailnet.ts.net,other.example",
    }
    if source == "environment":
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    else:
        (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    settings = get_settings()
    assert settings.dashboard_password.get_secret_value() == values["DASHBOARD_PASSWORD"]
    assert values["DASHBOARD_PASSWORD"] not in repr(settings)
    assert settings.dashboard_bind == "100.100.20.30"
    assert settings.dashboard_secure_cookie is True
    assert settings.dashboard_hostnames == "machine.tailnet.ts.net,other.example"


@pytest.mark.parametrize(
    "name,value",
    [
        ("DASHBOARD_SECURE_COOKIE", "perhaps"),
        ("DASHBOARD_HOSTNAMES", "*.example"),
        ("DASHBOARD_HOSTNAMES", "https://machine.example"),
        ("DASHBOARD_HOSTNAMES", "machine.example/path"),
        ("DASHBOARD_HOSTNAMES", "machine.example:8000"),
    ],
)
def test_invalid_dashboard_settings_are_sanitised(no_settings_file, monkeypatch, name, value):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name) as exc:
        get_settings()
    assert value not in str(exc.value)
