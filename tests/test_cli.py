import pytest
from sqlalchemy import text

from portfolio_digest import __version__
from portfolio_digest.cli import main
from portfolio_digest.validation import PORTFOLIO_NAME_MAX

UNREACHABLE = "postgresql+psycopg://user:s3cret-pass@127.0.0.1:1/nowhere"


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_health_fails_before_migrating(empty_engine, capsys):
    assert main(["health"]) == 1
    assert "Run `migrate`" in capsys.readouterr().err


def test_migrate_then_health_ok(empty_engine, capsys):
    assert main(["migrate"]) == 0
    assert main(["health"]) == 0
    assert "OK:" in capsys.readouterr().out


def test_migrate_on_an_up_to_date_database_changes_nothing(engine, capsys):
    assert main(["portfolio", "add", "Personal"]) == 0
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert main(["health"]) == 0
    capsys.readouterr()
    assert main(["portfolio", "list"]) == 0
    assert "Personal" in capsys.readouterr().out


def test_health_spots_a_database_from_a_newer_version(engine, capsys):
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffff'"))
    assert main(["health"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert main(["migrate"]) == 1
    assert "Migration failed" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["health"], ["migrate"], ["portfolio", "list"]])
def test_missing_database_url_is_a_clear_error(no_settings_file, capsys, command):
    assert main(command) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        ["health"],
        ["migrate"],
        ["portfolio", "list"],
        ["portfolio", "add", "X"],
        ["returns", "--portfolio", "Invented", "--period", "week"],
    ],
)
def test_an_unreachable_database_does_not_leak_credentials(
    no_settings_file, monkeypatch, capsys, command
):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE)
    assert main(command) == 1
    out = capsys.readouterr()
    assert "s3cret-pass" not in out.out + out.err
    assert "OperationalError" in out.err


def test_portfolio_list_before_migrating_points_at_health(empty_engine, capsys):
    assert main(["portfolio", "list"]) == 1
    assert "portfolio-digest health" in capsys.readouterr().err


def test_portfolio_add_and_list(engine, capsys):
    assert main(["portfolio", "add", "Family Trust"]) == 0
    assert main(["portfolio", "add", "Personal", "--base-currency", "aud"]) == 0
    assert main(["portfolio", "list"]) == 0
    out = capsys.readouterr().out
    assert out.index("Family Trust\tNZD") < out.index("Personal\tAUD")


def test_portfolio_add_defaults_to_the_base_currency_setting(engine, monkeypatch, capsys):
    monkeypatch.setenv("BASE_CURRENCY", "AUD")
    assert main(["portfolio", "add", "Personal"]) == 0
    assert "(AUD)" in capsys.readouterr().out


def test_portfolio_add_tidies_whitespace_so_list_stays_one_line_each(engine, capsys):
    assert main(["portfolio", "add", "Family\tTrust\n"]) == 0
    assert main(["portfolio", "list"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].split("\t")[1:] == ["Family Trust", "NZD"]


@pytest.mark.parametrize("duplicate", ["Personal", "personal", " PERSONAL "])
def test_portfolio_add_rejects_duplicate_names_ignoring_case(engine, capsys, duplicate):
    assert main(["portfolio", "add", "Personal"]) == 0
    assert main(["portfolio", "add", duplicate]) == 1
    assert "already a portfolio" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["   "],
        ["x" * (PORTFOLIO_NAME_MAX + 1)],
        ["Other", "--base-currency", ""],
        ["Other", "--base-currency", "nz"],
        ["Other", "--base-currency", "dollars"],
    ],
)
def test_portfolio_add_rejects_bad_input_without_touching_the_database(engine, capsys, args):
    assert main(["portfolio", "add", *args]) == 2
    out = capsys.readouterr()
    assert out.err and "Traceback" not in out.err
    assert main(["portfolio", "list"]) == 0
    assert "No portfolios yet" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "descriptions"),
    [
        (
            ["trade", "add"],
            [
                "Portfolio name",
                "Trade or action date",
                "Trade type",
                "Listing exchange",
                "Instrument code",
                "adjustments may be signed",
                "Price per unit",
                "ISO currency code",
                "Brokerage and fees",
                "Instrument name",
                "Private notes",
            ],
        ),
        (["scheduler"], ["Schedule configuration file path", "Scheduler state file path"]),
        (["alerts", "check"], ["Schedule configuration file path", "Preview without sending"]),
        (["alerts"], ["Check and send due alerts"]),
        (["dividends"], ["Reject a pending dividend suggestion"]),
        (["dividends", "reject"], ["Pending dividend suggestion ID"]),
    ],
)
def test_help_describes_options_without_a_database(no_settings_file, capsys, command, descriptions):
    with pytest.raises(SystemExit) as exc:
        main([*command, "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for description in descriptions:
        assert description in output
