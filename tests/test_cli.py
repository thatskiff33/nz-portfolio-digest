import pytest

from portfolio_digest import __version__
from portfolio_digest.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_health_fails_before_migrating(empty_engine, capsys):
    assert main(["health"]) == 1
    assert "Run `migrate`" in capsys.readouterr().out


def test_migrate_then_health_ok(empty_engine, capsys):
    assert main(["migrate"]) == 0
    assert main(["health"]) == 0
    assert "OK:" in capsys.readouterr().out


def test_health_does_not_leak_credentials(monkeypatch, capsys):
    from portfolio_digest.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:s3cret-pass@127.0.0.1:1/nowhere")
    get_settings.cache_clear()
    try:
        assert main(["health"]) == 1
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr()
    assert "s3cret-pass" not in out.out + out.err


def test_portfolio_add_and_list(engine, capsys):
    assert main(["portfolio", "add", "Family Trust"]) == 0
    assert main(["portfolio", "add", "Personal"]) == 0
    assert main(["portfolio", "list"]) == 0
    out = capsys.readouterr().out
    assert out.index("Family Trust") < out.index("Personal")


def test_portfolio_add_rejects_duplicates_and_blank_names(engine, capsys):
    assert main(["portfolio", "add", "Personal"]) == 0
    assert main(["portfolio", "add", "Personal"]) == 1
    assert main(["portfolio", "add", "   "]) == 2
    assert main(["portfolio", "add", "Other", "--base-currency", "nz"]) == 1
