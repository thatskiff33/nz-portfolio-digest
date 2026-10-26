"""Exercise the real SQL loader and CLI using entirely invented database rows."""

from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import CheckConstraint, MetaData, create_engine
from sqlalchemy.orm import Session

from portfolio_digest.cli import main
from portfolio_digest.config import get_settings
from portfolio_digest.models import Base, FxRate, Income, Instrument, Portfolio, Price, Trade
from portfolio_digest.returns import ReturnError
from portfolio_digest.returns_loader import load_returns


@pytest.fixture
def returns_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    # SQLite exercises SQL selection/mapping without a Postgres service. Only remove
    # Postgres-specific CHECK expressions; the existing schema suite tests constraints.
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.remove(constraint)
    metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Portfolio(id=1, name="Invented", base_currency="NZD"),
                Portfolio(id=2, name="Separate", base_currency="NZD"),
                Instrument(
                    id=1, exchange="NASDAQ", code="IMAGINARY", name="Imaginary", currency="USD"
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=date(2026, 10, 1),
                    trade_type="buy",
                    quantity=D(10),
                    price=D(10),
                    currency="USD",
                    fees=D(0),
                    source="made_up",
                    external_ref="buy",
                ),
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=date(2026, 10, 15),
                    trade_type="sell",
                    quantity=D(10),
                    price=D(12),
                    currency="USD",
                    fees=D(0),
                    fx_rate=D("1.6"),
                    source="made_up",
                    external_ref="sell",
                ),
                Trade(
                    portfolio_id=2,
                    instrument_id=1,
                    trade_date=date(2026, 10, 1),
                    trade_type="buy",
                    quantity=D(999),
                    price=D(10),
                    currency="USD",
                    fees=D(0),
                    source="made_up",
                    external_ref="other",
                ),
                # Must not leak future trades or use a future quote/FX for opening.
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=date(2026, 11, 1),
                    trade_type="buy",
                    quantity=D(500),
                    price=D(10),
                    currency="USD",
                    fees=D(0),
                    source="made_up",
                    external_ref="future",
                ),
                Price(
                    instrument_id=1, price_date=date(2026, 10, 12), close=D(10), source="made_up"
                ),
                Price(
                    instrument_id=1, price_date=date(2026, 11, 1), close=D(900), source="made_up"
                ),
                FxRate(
                    rate_date=date(2026, 10, 12),
                    currency="USD",
                    base_currency="NZD",
                    rate=D("1.5"),
                    source="made_up",
                ),
                FxRate(
                    rate_date=date(2026, 11, 1),
                    currency="USD",
                    base_currency="NZD",
                    rate=D(900),
                    source="made_up",
                ),
                Income(
                    portfolio_id=1,
                    instrument_id=1,
                    paid_date=date(2026, 10, 19),
                    kind="dividend",
                    currency="NZD",
                    gross_amount=D(5),
                    net_amount=D(4),
                    source="made_up",
                    external_ref="div",
                ),
                Income(
                    portfolio_id=1,
                    instrument_id=1,
                    paid_date=date(2026, 10, 19),
                    kind="interest",
                    currency="NZD",
                    gross_amount=D(99),
                    net_amount=D(99),
                    source="made_up",
                    external_ref="interest",
                ),
            ]
        )
        session.commit()
    yield engine
    engine.dispose()
    get_settings.cache_clear()


def test_loader_keeps_sold_holding_scopes_portfolio_and_uses_gross_income(returns_engine):
    with Session(returns_engine) as session:
        r = load_returns(session, " invented ", "2026-10-12:2026-10-19")
    assert len(r.holdings) == 1
    assert r.total.closing == 0
    assert r.total.opening == 150
    assert r.total.amounts.capital == 30
    assert r.total.amounts.currency == 12
    assert r.total.amounts.dividends == 5
    assert r.total.amounts.total == 47


def test_loader_errors_and_empty_portfolio(returns_engine):
    with Session(returns_engine) as session:
        with pytest.raises(ReturnError, match="not found"):
            load_returns(session, "Missing", "week")
        session.add(Portfolio(name="Empty", base_currency="NZD"))
        session.commit()
        r = load_returns(session, "Empty", "all")
        assert r.total.amounts.total == 0 and r.total.percentages is None


def test_cli_prints_made_up_portfolio(returns_engine, monkeypatch, capsys):
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: returns_engine)
    assert main(["returns", "--portfolio", "Invented", "--period", "2026-10-12:2026-10-19"]) == 0
    output = capsys.readouterr().out
    assert "Capital gain" in output and "Dividends" in output and "Currency gain" in output
    assert "Total return" in output and "NZ$" in output
    assert "NASDAQ:IMAGINARY" in output and "Portfolio" in output
    assert "47.00 (31.33%)" in output
    assert main(["returns", "--portfolio", "Invented", "--period", "bad"]) == 2
    assert "Period must be" in capsys.readouterr().err


def test_cli_flags_carried_forward_data(returns_engine, monkeypatch, capsys):
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: returns_engine)
    assert main(["returns", "--portfolio", "Separate", "--period", "2026-10-12:2026-10-19"]) == 0
    assert "Carried forward" in capsys.readouterr().out


def test_invalid_cli_arguments_do_not_need_database(no_settings_file, capsys):
    assert main(["returns", "--portfolio", "Invented", "--period", "bad"]) == 2
    assert "Period must be" in capsys.readouterr().err
    assert main(["returns", "--portfolio", "  ", "--period", "week"]) == 2
    assert "DATABASE_URL" not in capsys.readouterr().err


def test_configured_timezone_controls_today(returns_engine, monkeypatch):
    from datetime import datetime as RealDatetime
    from zoneinfo import ZoneInfo

    class FixedDatetime:
        @staticmethod
        def now(tz):
            assert tz == ZoneInfo("America/New_York")
            return RealDatetime(2026, 10, 19, 23, 30, tzinfo=tz)

    monkeypatch.setenv("TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    monkeypatch.setattr("portfolio_digest.returns_loader.datetime", FixedDatetime)
    with Session(returns_engine) as session:
        r = load_returns(session, "Invented", "week")
        assert (r.start, r.end) == (date(2026, 10, 12), date(2026, 10, 19))
