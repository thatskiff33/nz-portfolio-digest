import subprocess
import sys

import pytest
from sqlalchemy import text

from portfolio_digest import __version__
from portfolio_digest.cli import main
from portfolio_digest.validation import PORTFOLIO_NAME_MAX

UNREACHABLE = "postgresql+psycopg://user:s3cret-pass@127.0.0.1:1/nowhere?connect_timeout=2"


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


@pytest.mark.parametrize("ok", [False, True])
def test_manual_price_fetch_records_failure_and_recovery(monkeypatch, ok):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from portfolio_digest import cli

    report = SimpleNamespace(
        ok=ok,
        outcomes=[
            SimpleNamespace(
                error=None if ok else "private provider error 123",
                label="NZX:MADEUP",
                describe=lambda: "fetch result",
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(price_source="fake", fx_source_names=[], base_currency="NZD"),
    )
    monkeypatch.setitem(cli.PRICE_SOURCES, "fake", lambda _: None)
    monkeypatch.setattr(cli, "make_client", lambda: nullcontext(None))
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "run_fetch", lambda *a, **kw: report)
    config = object()
    monkeypatch.setattr(cli, "load_notice_config", lambda: config)
    calls = []
    monkeypatch.setattr(cli, "note_outcome", lambda *a, **kw: calls.append((a, kw)))
    assert cli.cmd_prices_fetch(SimpleNamespace(backfill=False, start=None, instrument=None)) == (
        0 if ok else 1
    )
    assert calls == [
        ((config, "prices"), {"failed": not ok, "labels": () if ok else ("NZX:MADEUP",)})
    ]


def test_main_suppresses_http_request_details(monkeypatch, caplog):
    import logging
    from argparse import Namespace

    import httpx

    from portfolio_digest import cli

    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.NOTSET)

    def fetch(_):
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ) as client:
            client.get("https://prices.example.invalid/INVENTED?period1=123456")
        logging.getLogger("httpcore").info("INVENTED holding details")
        for logger in loggers:
            logger.warning("Transport warning")
        return 0

    monkeypatch.setattr(
        cli, "build_parser", lambda: Namespace(parse_args=lambda _: Namespace(func=fetch))
    )
    try:
        with caplog.at_level(logging.INFO):
            assert main([]) == 0
        assert "INVENTED" not in caplog.text
        assert not any(
            r.name in {"httpx", "httpcore"} and r.levelno == logging.INFO for r in caplog.records
        )
        assert sum(r.message == "Transport warning" for r in caplog.records) == 2
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def test_cli_imports_without_fcntl():
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl'] = None; import portfolio_digest.cli",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dividend_suggestion_default_date_is_resolved_when_command_runs(monkeypatch):
    from contextlib import nullcontext
    from datetime import UTC, date, datetime

    from sqlalchemy.orm import Session
    from test_returns_loader import _sqlite_engine

    from portfolio_digest import cli, config
    from portfolio_digest.models import Instrument, Portfolio

    class ParserDate(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 6)

    # Build once, then cross Auckland midnight before running the command.
    monkeypatch.setattr(cli, "date", ParserDate)
    args = cli.build_parser().parse_args(
        [
            "dividends",
            "suggest",
            "--portfolio",
            "Example",
            "--instrument",
            "NZX:FERN",
            "--start",
            "2026-01-01",
        ]
    )
    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Portfolio(name="Example"),
                Instrument(exchange="NZX", code="FERN", name="Fern", currency="NZD"),
            ]
        )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 6, 12, tzinfo=UTC).astimezone(tz)

    class Source:
        def symbol(self, listing):
            return listing.code

        def dividends(self, symbol, start, end, currency):
            assert end == date(2026, 11, 7)
            return {}

    monkeypatch.setenv("TIMEZONE", "Pacific/Auckland")
    monkeypatch.setattr(config, "datetime", Clock)
    monkeypatch.setattr(cli, "make_engine", lambda: engine)
    monkeypatch.setattr(cli, "make_client", lambda: nullcontext(None))
    monkeypatch.setattr(cli, "YahooSource", lambda fetcher: Source())
    try:
        assert args.func(args) == 0
    finally:
        engine.dispose()
        config.get_settings.cache_clear()
