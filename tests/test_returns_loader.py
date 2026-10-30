"""Exercise the real SQL loader and CLI using entirely invented database rows."""

from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session

from portfolio_digest.cli import main
from portfolio_digest.config import get_settings
from portfolio_digest.models import (
    Base,
    FxRate,
    Income,
    Instrument,
    Portfolio,
    Price,
    Split,
    Trade,
)
from portfolio_digest.returns import ReturnError
from portfolio_digest.returns_loader import load_returns


def _sqlite_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    # SQLite runs without a Postgres service. Only Postgres-specific CHECK expressions
    # are removed; the Postgres run below keeps every constraint.
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.remove(constraint)
    metadata.create_all(engine)
    return engine


@pytest.fixture(params=["sqlite", "postgres"])
def returns_engine(request, monkeypatch):
    if request.param == "postgres":
        # The migrated throwaway Postgres from conftest; skipped when none is configured.
        engine = request.getfixturevalue("engine")
    else:
        engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        invented = Portfolio(name="Invented", base_currency="NZD")
        separate = Portfolio(name="Separate", base_currency="NZD")
        imaginary = Instrument(
            exchange="NASDAQ", code="IMAGINARY", name="Imaginary", currency="USD"
        )
        session.add_all([invented, separate, imaginary])
        session.flush()
        one, two, instrument = invented.id, separate.id, imaginary.id
        session.add_all(
            [
                Trade(
                    portfolio_id=one,
                    instrument_id=instrument,
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
                    portfolio_id=one,
                    instrument_id=instrument,
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
                    portfolio_id=two,
                    instrument_id=instrument,
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
                    portfolio_id=one,
                    instrument_id=instrument,
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
                    instrument_id=instrument,
                    price_date=date(2026, 10, 12),
                    close=D(10),
                    source="made_up",
                ),
                Price(
                    instrument_id=instrument,
                    price_date=date(2026, 11, 1),
                    close=D(900),
                    source="made_up",
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
                    portfolio_id=one,
                    instrument_id=instrument,
                    paid_date=date(2026, 10, 19),
                    kind="dividend",
                    currency="NZD",
                    gross_amount=D(5),
                    net_amount=D(4),
                    source="made_up",
                    external_ref="div",
                ),
                Income(
                    portfolio_id=one,
                    instrument_id=instrument,
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
    if request.param == "sqlite":
        engine.dispose()
        get_settings.cache_clear()


def test_loader_keeps_sold_holding_scopes_portfolio_and_uses_net_income(returns_engine):
    with Session(returns_engine) as session:
        r = load_returns(session, " invented ", "2026-10-12:2026-10-19")
    assert len(r.holdings) == 1
    assert r.total.closing == 0
    assert r.total.opening == 150
    assert r.total.amounts.capital == 30
    assert r.total.amounts.currency == 12
    assert r.total.amounts.dividends == 4
    assert r.total.amounts.total == 46


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
    assert "46.00 (30.67%)" in output
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


def test_cli_escapes_control_characters_in_labels_flags_and_errors(
    returns_engine, monkeypatch, capsys
):
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: returns_engine)
    with Session(returns_engine) as session:
        session.scalar(select(Instrument)).code = "FERN\x1b[2J"
        session.commit()
    args = ["returns", "--portfolio", "Separate", "--period", "2026-10-12:2026-10-19"]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "FERN\\x1b[2J" in output
    assert "Carried forward FERN\\x1b[2J price" in output

    with Session(returns_engine) as session:
        # Force an oversell error which includes the untrusted instrument code.
        sale = session.scalar(select(Trade).where(Trade.external_ref == "sell"))
        sale.quantity = D(11)
        session.commit()
    args[2] = "Invented"
    assert main(args) == 2
    error = capsys.readouterr().err
    assert "\x1b" not in error
    assert "FERN\\x1b[2J" in error and "exceeds" in error


def test_loader_applies_splits_and_non_cash_adjustments_and_drops_old_sales(returns_engine):
    with Session(returns_engine) as session:
        portfolio = Portfolio(name="Actions", base_currency="NZD")
        kauri = Instrument(exchange="NZX", code="KAURI", name="Kauri", currency="NZD")
        gone = Instrument(exchange="NZX", code="GONE", name="Gone", currency="NZD")
        session.add_all([portfolio, kauri, gone])
        session.flush()

        def row(instrument, day, kind, quantity, price, ref):
            return Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=day,
                trade_type=kind,
                quantity=D(quantity),
                price=D(price),
                currency="NZD",
                fees=D(0),
                source="made_up",
                external_ref=ref,
            )

        session.add_all(
            [
                row(kauri, date(2026, 1, 5), "buy", "10", "10", "k-buy"),
                # Pre-period transfer out counts towards the opening quantity.
                row(kauri, date(2026, 3, 2), "adjustment", "-2", "9", "k-out"),
                # 8 units split 2-for-1 into 16, then a non-cash DRP of 1 unit at 6.
                row(kauri, date(2026, 10, 14), "adjustment", "8", "0", "k-split"),
                row(kauri, date(2026, 10, 15), "adjustment", "1", "6", "k-drp"),
                row(gone, date(2026, 1, 5), "buy", "5", "1", "g-buy"),
                row(gone, date(2026, 2, 5), "sell", "5", "1", "g-sell"),
                Split(
                    instrument_id=kauri.id,
                    split_date=date(2026, 10, 14),
                    shares_before=D(1),
                    shares_after=D(2),
                    source="made_up",
                ),
                Price(
                    instrument_id=kauri.id, price_date=date(2026, 10, 12), close=D(12), source="x"
                ),
                Price(
                    instrument_id=kauri.id, price_date=date(2026, 10, 19), close=D(7), source="x"
                ),
            ]
        )
        session.commit()
        r = load_returns(session, "Actions", "2026-10-12:2026-10-19")
    assert [h.label for h in r.holdings] == ["NZX:KAURI"]
    # Opening 8 x 12 = 96. Closing 17 x 7 = 119. DRP brought in 6 of value.
    assert (r.total.opening, r.total.closing, r.total.adjustments) == (D(96), D(119), D(6))
    # Split units: 16 x (7 - 6) = 16. DRP unit: 7 - 6 = 1.
    assert r.total.amounts.capital == 17 and r.total.flags == ()
    assert r.total.percentages.total.quantize(D(".01")) == D("17.71")


@pytest.mark.parametrize(
    "before,after,units,close", [("1", "2", "20", "5"), ("10", "1", "1", "100")]
)
@pytest.mark.parametrize("with_adjustment", [False, True])
def test_registry_action_reports_and_reconciles_once(
    monkeypatch, before, after, units, close, with_adjustment
):
    from portfolio_digest.actions import record_action
    from portfolio_digest.reconcile import ledger_units, stored_units
    from portfolio_digest.returns import report

    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        portfolio = Portfolio(name="Registry", base_currency="NZD")
        instrument = Instrument(exchange="NZX", code="EXAMPLE", name="Example", currency="NZD")
        session.add_all([portfolio, instrument])
        session.flush()
        session.add(
            Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=date(2024, 1, 1),
                trade_type="buy",
                quantity=D(10),
                price=D(10),
                currency="NZD",
                source="fixture",
                external_ref="initial",
            )
        )
        session.add(
            Price(
                instrument_id=instrument.id,
                price_date=date(2024, 1, 1),
                close=D(10),
                source="fixture",
            )
        )
        kind = "split" if D(after) > D(before) else "consolidation"
        record_action(
            session,
            instrument,
            date(2024, 6, 1),
            kind,
            "registry-action",
            {"before": before, "after": after},
        )
        if with_adjustment:
            session.add(
                Trade(
                    portfolio_id=portfolio.id,
                    instrument_id=instrument.id,
                    trade_date=date(2024, 6, 1),
                    trade_type="adjustment",
                    quantity=D(units) - D(10),
                    price=D(0),
                    currency="NZD",
                    source="fixture",
                    external_ref="adjustment",
                )
            )
        session.flush()
        # Carry the old raw price across the action, then use a fresh post-action close.
        for fresh_price in (False, True):
            if fresh_price:
                session.add(
                    Price(
                        instrument_id=instrument.id,
                        price_date=date(2024, 6, 20),
                        close=D(close),
                        source="fixture",
                    )
                )
                session.flush()
            for period in ("all", "2024-06-10:2024-06-20"):
                result = load_returns(session, portfolio.name, period, date(2024, 6, 20))
                assert result.total.closing == 100
                assert result.total.amounts.total == 0
            lifetime = report(session, portfolio, date(2024, 6, 20))[0]
            assert (lifetime.units, lifetime.cost_base, lifetime.total_return) == (D(units), 100, 0)
            assert ledger_units(session, portfolio.id) == {instrument.id: D(units)}
            assert stored_units(session, portfolio.id) == {("NZX", "EXAMPLE"): D(units)}
    engine.dispose()


def test_shared_report_net_tax_drp_and_gross_up(monkeypatch, capsys):
    from dataclasses import replace

    from portfolio_digest.income import parse_income, store_income
    from portfolio_digest.returns import report

    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        portfolio = Portfolio(name="DRP", base_currency="NZD")
        instrument = Instrument(exchange="NZX", code="ETFEXAMPLE", name="Example", currency="NZD")
        session.add_all([portfolio, instrument])
        session.flush()
        income = parse_income(
            "fixture",
            {
                "date": "2024-06-20",
                "gross": "100",
                "net": "90",
                "rwt": "10",
                "imputation": "28",
            },
            "NZX",
            instrument.code,
            "NZD",
            "dividend",
        )
        store_income(
            session, portfolio.id, instrument.id, replace(income, drp_quantity=D(3)), "registry"
        )
        session.add(
            Price(
                instrument_id=instrument.id,
                price_date=date(2024, 6, 20),
                close=D(30),
                source="fixture",
            )
        )
        session.commit()
        for gross_up, total in ((False, 90), (True, 118)):
            result = load_returns(
                session, portfolio.name, "all", date(2024, 6, 20), gross_up=gross_up
            )
            assert (result.total.closing, result.total.purchases) == (90, 90)
            assert result.total.amounts.dividends == 90
            assert result.total.amounts.total == total
            assert result.total.gross_dividends == 100
            assert result.total.withholding_tax == 10
            assert result.total.imputation_gross_up == 28
            assert (
                report(session, portfolio, date(2024, 6, 20), gross_up=gross_up)[0].total_return
                == total
            )
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: engine)
    assert main(["returns", "--portfolio", "DRP", "--date", "2024-06-20", "--gross-up"]) == 0
    output = capsys.readouterr().out
    assert "Imputation gross-up" in output and "118.00" in output
    engine.dispose()


def test_all_period_includes_income_without_trades(monkeypatch):
    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        portfolio = Portfolio(name="Income only", base_currency="NZD")
        instrument = Instrument(exchange="NZX", code="EXAMPLE", name="Example", currency="NZD")
        session.add_all([portfolio, instrument])
        session.flush()
        session.add(
            Income(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                paid_date=date(2024, 6, 20),
                kind="dividend",
                currency="NZD",
                gross_amount=D(100),
                net_amount=D(90),
                source="fixture",
                external_ref="dividend",
            )
        )
        session.flush()
        result = load_returns(session, portfolio.name, "all", date(2024, 12, 31))
        assert result.total.amounts.dividends == 90
        assert result.total.percentages is None
    engine.dispose()


def test_cli_keeps_corporate_action_command_after_dashboard_merge(monkeypatch, capsys):
    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        session.add(Portfolio(name="Rights example", base_currency="NZD"))
        session.commit()
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: engine)
    assert (
        main(
            [
                "corporate-actions",
                "rights_entitlement",
                "NZX:EXAMPLERG",
                "--date",
                "2026-10-20",
                "--reference",
                "fictional-entitlement",
                "--portfolio",
                "Rights example",
                "--details",
                '{"quantity": "10"}',
            ]
        )
        == 0
    )
    assert "Recorded corporate action" in capsys.readouterr().out
    with Session(engine) as session:
        instrument = session.scalar(select(Instrument).where(Instrument.code == "EXAMPLERG"))
        assert instrument.kind == "rights"
        trade = session.scalar(select(Trade).where(Trade.instrument_id == instrument.id))
        assert trade.quantity == D(10)
    engine.dispose()


def test_email_engine_and_cli_figures_are_identical(returns_engine, monkeypatch, capsys):
    from portfolio_digest.reports import build_report, render
    from portfolio_digest.returns_table import render_table

    # Keep one fictional portfolio so the all-portfolio email and scoped CLI agree.
    with Session(returns_engine) as session:
        separate = session.scalar(select(Portfolio).where(Portfolio.name == "Separate"))
        for row in session.scalars(select(Trade).where(Trade.portfolio_id == separate.id)):
            session.delete(row)
        income = session.scalar(select(Income).where(Income.external_ref == "div"))
        income.withholding_tax = D(1)
        income.imputation_credits = D(".7")
        session.commit()
        shared = load_returns(session, "Invented", "2026-10-12:2026-10-19")
        email = build_report(session, date(2026, 10, 13), date(2026, 10, 19))
    assert email.returns.holdings == shared.holdings
    assert email.returns.total == shared.total
    for row, expected in zip(email.holdings, shared.holdings, strict=True):
        assert (
            row.amounts.capital,
            row.amounts.dividends,
            row.amounts.currency,
            row.amounts.total,
        ) == (
            expected.amounts.capital,
            expected.amounts.dividends,
            expected.amounts.currency,
            expected.amounts.total,
        )
    assert email.total_return == shared.total.amounts.total == D(46)
    assert email.returns.total.gross_dividends == D(5)
    assert email.returns.total.withholding_tax == D(1)
    assert email.returns.total.imputation_gross_up == D(".7")
    assert email.history[0].value == shared.total.opening
    assert email.history[-1].value == shared.total.closing
    assert email.history[0].day == shared.start
    _, text = render(email)
    assert "Gross dividends" in text and "Withholding tax" in text
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: returns_engine)
    assert main(["returns", "--portfolio", "Invented", "--period", "2026-10-12:2026-10-19"]) == 0
    assert capsys.readouterr().out == render_table(email.returns) + "\n"


def test_email_combines_listings_and_converts_foreign_portfolio_broker_fx(returns_engine):
    from portfolio_digest.reports import build_report

    with Session(returns_engine) as session:
        separate = session.scalar(select(Portfolio).where(Portfolio.name == "Separate"))
        separate.base_currency = "USD"
        instrument = session.scalar(select(Instrument))
        old = session.scalar(select(Trade).where(Trade.external_ref == "other"))
        old.quantity = D(10)
        old.fx_rate = D(1)
        session.add_all(
            [
                Trade(
                    portfolio_id=separate.id,
                    instrument_id=instrument.id,
                    trade_date=date(2026, 10, 15),
                    trade_type="buy",
                    quantity=D(2),
                    price=D(12),
                    fees=D(0),
                    currency="USD",
                    fx_rate=D(1),
                    source="made_up",
                    external_ref="foreign-buy",
                ),
                Income(
                    portfolio_id=separate.id,
                    instrument_id=instrument.id,
                    paid_date=date(2026, 10, 15),
                    kind="dividend",
                    currency="USD",
                    gross_amount=D(3),
                    net_amount=D(2),
                    withholding_tax=D(1),
                    imputation_credits=D(".5"),
                    fx_rate=D(1),
                    source="made_up",
                    external_ref="foreign-income",
                ),
            ]
        )
        session.commit()
        email = build_report(session, date(2026, 10, 13), date(2026, 10, 19))
    (row,) = email.holdings
    # Invented realises 30 capital + 12 FX + 4 dividend. Separate buys two
    # units at 12, closes at 10: -4 USD capital + 2 USD net dividend.
    assert row.units == 12 and row.price == 10 and row.currency == "USD"
    assert row.name == "Imaginary"
    assert row.opening == 300 and row.closing == 180
    assert row.amounts.capital == 24
    assert row.amounts.currency == 12
    assert row.amounts.dividends == 7
    assert row.amounts.total == 43
    assert row.gross_dividends == D("9.5")
    assert row.withholding_tax == D("1.5")
    assert row.imputation_gross_up == D(".75")
    assert row.flags  # Includes stale conversion quotes.
    assert email.returns.total.amounts == row.amounts


def test_email_missing_history_fails_safely(returns_engine):
    from portfolio_digest.reports import ReportError, build_report

    with Session(returns_engine) as session:
        for row in session.scalars(select(Price)):
            session.delete(row)
        session.commit()
        with pytest.raises(ReportError, match="prices fetch --backfill") as error:
            build_report(session, date(2026, 10, 13), date(2026, 10, 19))
        assert "IMAGINARY" not in str(error.value)
        with pytest.raises(ReportError, match="start"):
            build_report(session, date(2026, 10, 20), date(2026, 10, 19))


@pytest.mark.parametrize("broker_fx", [D(".61"), None])
def test_email_keeps_nzd_trades_and_income_native_in_foreign_portfolio(returns_engine, broker_fx):
    from portfolio_digest.reports import build_report

    with Session(returns_engine) as session:
        foreign = session.scalar(select(Portfolio).where(Portfolio.name == "Separate"))
        foreign.base_currency = "USD"
        for trade in session.scalars(select(Trade).where(Trade.portfolio_id == foreign.id)):
            session.delete(trade)
        instrument = Instrument(
            exchange="NZX", code="NATIVE", name="Native Example", currency="NZD"
        )
        session.add(instrument)
        session.flush()
        session.add_all(
            [
                Trade(
                    portfolio_id=foreign.id,
                    instrument_id=instrument.id,
                    trade_date=date(2026, 10, 15),
                    trade_type="buy",
                    quantity=D(10),
                    price=D(10),
                    fees=D(0),
                    currency="NZD",
                    fx_rate=broker_fx,
                    source="made_up",
                    external_ref="native-buy",
                ),
                Income(
                    portfolio_id=foreign.id,
                    instrument_id=instrument.id,
                    paid_date=date(2026, 10, 15),
                    kind="dividend",
                    currency="NZD",
                    gross_amount=D(5),
                    net_amount=D(4),
                    withholding_tax=D(1),
                    imputation_credits=D(".5"),
                    fx_rate=broker_fx,
                    source="made_up",
                    external_ref="native-income",
                ),
                Price(
                    instrument_id=instrument.id,
                    price_date=date(2026, 10, 15),
                    close=D(12),
                    source="made_up",
                ),
            ]
        )
        session.commit()
        # The USD/NZD market rate is 1.5, so .61 broker FX is not its reciprocal.
        # Neither rate should affect native NZD cash flows.
        email = build_report(session, date(2026, 10, 15), date(2026, 10, 19))
        row = next(r for r in email.holdings if r.label == "NZX:NATIVE")
        assert row.closing == D(120)
        assert row.amounts.capital == D(20)
        assert row.amounts.dividends == D(4)
        assert row.amounts.currency == D(0)
        assert row.amounts.total == D(24)
        assert row.gross_dividends == D(5)
        assert row.withholding_tax == D(1)
        assert row.imputation_gross_up == D(".5")

        # Native NZD rows also work when no foreign market quote is available.
        for quote in session.scalars(select(FxRate)):
            session.delete(quote)
        session.flush()
        native = load_returns(session, "Separate", "2026-10-14:2026-10-19", base_currency="NZD")
        assert native.holdings == (row,)


def test_named_and_portfolio_loaders_share_complete_inputs(returns_engine):
    from portfolio_digest.returns_loader import PortfolioInputs, load_inputs, load_portfolio_inputs

    with Session(returns_engine) as session:
        portfolio = session.scalar(select(Portfolio).where(Portfolio.name == "Invented"))
        start, end = date(2026, 10, 12), date(2026, 10, 19)
        named = load_inputs(session, "invented", f"{start}:{end}")
        direct = load_portfolio_inputs(session, portfolio, start, end)
        assert isinstance(named, PortfolioInputs)
        assert named == direct
        assert named.start == start and named.end == end
        assert named.income and named.instruments and named.holdings and named.rates
        assert named.holdings[0].income[0].amount == D(4)
