"""Hand-worked, entirely fictional NZ, US and AU tax-year ledger."""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session
from test_returns_loader import _sqlite_engine

from portfolio_digest.cli import main
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.returns import Holding, Observation, ReturnError, SplitEvent, Transaction
from portfolio_digest.year_end import Dividend, calculate_year_end
from portfolio_digest.year_end_delivery import (
    MailSettings,
    build_message,
    deliver,
    latest_ended_year,
    load_year_end,
)
from portfolio_digest.year_end_report import render_csv, render_html


def day(value):
    return date.fromisoformat(value)


def trade(when, kind, quantity, price, fees="0", fx=None):
    return Transaction(day(when), kind, D(quantity), D(price), D(fees), D(fx) if fx else None)


@pytest.fixture
def ledger():
    nz = Holding(
        "NZX",
        "FERN",
        "NZD",
        (
            trade("2024-05-01", "buy", "100", "10", "10"),
            trade("2025-05-01", "adjustment", "100", "0"),
            trade("2025-06-01", "sell", "50", "8", "5"),
            trade("2025-09-01", "adjustment", "2", "8"),
        ),
        (Observation(day("2025-03-31"), D(12)), Observation(day("2026-03-31"), D(9))),
        splits=(SplitEvent(day("2025-05-01"), D(1), D(2)),),
    )
    us = Holding(
        "NASDAQ",
        "CLOUD",
        "USD",
        (
            trade("2025-06-01", "buy", "10", "20", "2", "1.5"),
            trade("2025-07-01", "sell", "10", "15", "1", "2"),
        ),
    )
    au = Holding(
        "ASX",
        "GUM",
        "AUD",
        (
            trade("2025-08-01", "buy", "100", "400", fx="1.1"),
            trade("2025-11-01", "buy", "20", "400", fx="1.1"),
        ),
        (Observation(day("2026-03-31"), D(450)),),
    )
    dividends = (
        Dividend("NZX:FERN", day("2026-03-31"), "NZD", D(40), D(10), D(8), D(32)),
        Dividend("NASDAQ:CLOUD", day("2025-07-01"), "USD", D(10), D(0), D("1.5"), D("8.5"), D(2)),
        Dividend("ASX:GUM", day("2026-03-31"), "AUD", D(30), D(0), D(0), D(30), D("1.2")),
        Dividend("NZX:FERN", day("2025-03-31"), "NZD", D(999), D(0), D(0), D(999)),
        Dividend("NZX:FERN", day("2026-04-01"), "NZD", D(999), D(0), D(0), D(999)),
    )
    return (nz, us, au), dividends, {"AUD": (Observation(day("2026-03-31"), D("1.2")),)}


def test_every_figure_by_hand(ledger):
    report = calculate_year_end(*ledger, 2026)
    rows = {r.label: r for r in report.rows}
    # NZ split preserves $1,010 cost. 50/200 sold: 395 proceeds - 252.50 cost.
    nz = rows["NZX:FERN"]
    assert (nz.gains, nz.losses, nz.gross, nz.imputation, nz.resident, nz.foreign, nz.net) == (
        D("142.5"),
        D(0),
        D(40),
        D(10),
        D(8),
        D(0),
        D(32),
    )
    # US: 10*20*1.5 + 2*1.5 = 303 cost; (150-1)*2 = 298 proceeds.
    us = rows["NASDAQ:CLOUD"]
    assert (us.gains, us.losses, us.gross, us.imputation, us.resident, us.foreign, us.net) == (
        D(0),
        D(-5),
        D(20),
        D(0),
        D(0),
        D(3),
        D(17),
    )
    au = rows["ASX:GUM"]
    assert (au.gains, au.losses, au.gross, au.imputation, au.resident, au.foreign, au.net) == (
        D(0),
        D(0),
        D(36),
        D(0),
        D(0),
        D(0),
        D(36),
    )
    total = report.total
    assert (
        total.gains,
        total.losses,
        total.gross,
        total.imputation,
        total.resident,
        total.foreign,
        total.net,
    ) == (D("142.5"), D(-5), D(96), D(10), D(8), D(3), D(85))
    performance = {r.label: r for r in report.performance.holdings}
    assert (
        performance["NZX:FERN"].opening,
        performance["NZX:FERN"].closing,
        performance["NZX:FERN"].amounts.total,
    ) == (D(1200), D(1368), D(587))
    assert performance["NASDAQ:CLOUD"].amounts.total == D(15)
    assert performance["ASX:GUM"].amounts.total == D(12036)
    p = report.performance.total
    # 66,168 close + 693 sales - 1,200 open - 53,103 buys - 16 DRP + 96 gross income.
    assert (p.opening, p.closing, p.purchases, p.sales, p.adjustments, p.amounts.total) == (
        D(1200),
        D(66168),
        D(53103),
        D(693),
        D(16),
        D(12638),
    )
    assert p.percentages.total.quantize(D(".01")) == D("23.27")
    assert (report.foreign_peak, report.foreign_peak_day, report.fif_exceeded) == (
        D(52800),
        day("2025-11-01"),
        True,
    )


def test_fif_tracks_opening_cost_and_intrayear_peak_not_market_value():
    h = Holding(
        "ASX",
        "GUM",
        "AUD",
        (
            trade("2024-01-01", "buy", "100", "500", fx="1"),
            trade("2025-05-01", "sell", "100", "1", fx="1"),
        ),
        (Observation(day("2025-03-31"), D(2)),),
    )
    rates = {"AUD": (Observation(day("2025-03-31"), D(1)),)}
    r = calculate_year_end((h,), (), rates, 2026)
    assert r.foreign_peak == D(50000) and not r.fif_exceeded
    assert r.foreign_peak_day == day("2025-03-31")
    assert r.total.losses == D(-49900) and r.performance.total.opening == D(200)


def test_outputs_escape_html_and_preserve_csv_figures(ledger):
    r = calculate_year_end(*ledger, 2026)
    html = render_html(r, '<script>alert("x")</script>')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "information, not tax advice" in html and "FIFO" in html
    assert "Over the NZ$50,000" in html and "52,800.00" in html
    csv_rows = list(csv.reader(io.StringIO(render_csv(r))))
    portfolio = next(row for row in csv_rows if row[:1] == ["Portfolio"])
    assert list(map(D, portfolio[1:])) == [D("142.5"), D(-5), D(96), D(10), D(8), D(3), D(85)]
    message = build_message(
        r, "Invented", MailSettings(sender="a@example.invalid", recipient="b@example.invalid")
    )
    assert message.get_body(preferencelist=("html",)).get_content_type() == "text/html"
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "year-end-2026.csv"
    assert attachment.get_payload(decode=True).decode() == render_csv(r)


@pytest.fixture
def year_engine(monkeypatch, ledger):
    engine = _sqlite_engine(monkeypatch)
    holdings, dividends, rates = ledger
    with Session(engine) as session:
        portfolio = Portfolio(name="Invented", base_currency="NZD")
        session.add(portfolio)
        session.flush()
        ids = {}
        for h in holdings:
            instrument = Instrument(
                exchange=h.exchange, code=h.code, name="Invented", currency=h.currency
            )
            session.add(instrument)
            session.flush()
            ids[f"{h.exchange}:{h.code}"] = instrument.id
            for i, t in enumerate(h.trades):
                session.add(
                    Trade(
                        portfolio_id=portfolio.id,
                        instrument_id=instrument.id,
                        trade_date=t.day,
                        trade_type=t.kind,
                        quantity=t.quantity,
                        price=t.price,
                        fees=t.fees,
                        currency=h.currency,
                        fx_rate=t.fx,
                        source="invented",
                        external_ref=f"{h.code}-{i}",
                    )
                )
            for p in h.prices:
                session.add(
                    Price(
                        instrument_id=instrument.id,
                        price_date=p.day,
                        close=p.value,
                        source="invented",
                    )
                )
            for s in h.splits:
                session.add(
                    Split(
                        instrument_id=instrument.id,
                        split_date=s.day,
                        shares_before=s.before,
                        shares_after=s.after,
                        source="invented",
                    )
                )
        for i, p in enumerate(dividends):
            session.add(
                Income(
                    portfolio_id=portfolio.id,
                    instrument_id=ids[p.holding],
                    paid_date=p.day,
                    currency=p.currency,
                    gross_amount=p.gross,
                    imputation_credits=p.imputation,
                    withholding_tax=p.withholding,
                    net_amount=p.net,
                    fx_rate=p.fx,
                    source="invented",
                    external_ref=str(i),
                    drp_quantity=D(2) if i == 0 else None,
                )
            )
        for currency, points in rates.items():
            for p in points:
                session.add(
                    FxRate(
                        currency=currency,
                        base_currency="NZD",
                        rate_date=p.day,
                        rate=p.value,
                        source="invented",
                    )
                )
        session.commit()
    yield engine
    engine.dispose()


def test_database_loader_and_cli_dry_run_never_sends(year_engine, monkeypatch, tmp_path, ledger):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted SMTP")

    monkeypatch.setattr("portfolio_digest.year_end_delivery.smtplib.SMTP", forbidden)
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    with Session(year_engine) as session:
        loaded = load_year_end(session, "Invented", 2026)
    assert loaded.total == calculate_year_end(*ledger, 2026).total
    assert loaded.performance.total.amounts.total == D(12638)
    assert (
        main(
            [
                "year-end",
                "--portfolio",
                "Invented",
                "--year",
                "2026",
                "--dry-run",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "12638" in (tmp_path / "year-end-2026.csv").read_text()
    assert "Invented" in (tmp_path / "year-end-2026.html").read_text()
    with Session(year_engine) as session:
        session.query(Portfolio).filter_by(name="Invented").one().base_currency = "USD"
        session.commit()
        with pytest.raises(ReturnError, match="NZD"):
            load_year_end(session, "Invented", 2026)


def test_schedule_year_uses_auckland_boundary():
    assert latest_ended_year(datetime(2026, 3, 31, 10, 59, tzinfo=UTC)) == 2025
    assert latest_ended_year(datetime(2026, 3, 31, 11, 0, tzinfo=UTC)) == 2026
    assert latest_ended_year(datetime(2026, 4, 1, 0, tzinfo=UTC)) == 2026


def test_invalid_cli_and_missing_data_do_not_send(monkeypatch, ledger, tmp_path):
    assert main(["year-end", "--portfolio", "Invented", "--year", "1"]) == 2
    assert main(["year-end", "--portfolio", "Invented"]) == 2
    assert main(["year-end", "--portfolio", "Invented", "--year", "2026", "--dry-run"]) == 2
    h, dividends, _ = ledger
    with pytest.raises(ReturnError, match="Missing"):
        calculate_year_end(h, dividends, {}, 2026)
    with pytest.raises(ReturnError, match="--out"):
        deliver(calculate_year_end(*ledger, 2026), "Invented", dry_run=True)


def test_delivery_starttls_attachment_and_sanitised_failure(ledger, monkeypatch):
    r = calculate_year_end(*ledger, 2026)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_SENDER", "a@example.invalid")
    monkeypatch.setenv("SMTP_RECIPIENT", "b@example.invalid")
    monkeypatch.setenv("SMTP_USERNAME", "invented")
    monkeypatch.setenv("SMTP_PASSWORD", "invented-password")
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == "smtp.example.invalid" and port == 587 and timeout == 30

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, *, context):
            assert context.check_hostname
            calls.append("tls")

        def login(self, username, password):
            assert calls == ["tls"] and password == "invented-password"  # noqa: S105
            calls.append("login")

        def send_message(self, message):
            assert calls == ["tls", "login"]
            assert len(list(message.iter_attachments())) == 1
            calls.append("sent")

    monkeypatch.setattr("portfolio_digest.year_end_delivery.smtplib.SMTP", FakeSMTP)
    deliver(r, "Invented", dry_run=False)
    assert calls == ["tls", "login", "sent"]

    def broken(*args, **kwargs):
        raise OSError("invented-password")

    monkeypatch.setattr("portfolio_digest.year_end_delivery.smtplib.SMTP", broken)
    from portfolio_digest.config import ConfigError

    with pytest.raises(ConfigError, match="could not be sent") as exc:
        deliver(r, "Invented", dry_run=False)
    assert "invented-password" not in str(exc.value)


def test_all_portfolios_continue_after_one_fails(year_engine, monkeypatch, tmp_path):
    with Session(year_engine) as session:
        session.add(Portfolio(name="Empty", base_currency="NZD"))
        session.query(Portfolio).filter_by(name="Invented").one().base_currency = "USD"
        session.commit()
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    assert main(["year-end", "--all", "--year", "2026", "--dry-run", "--out", str(tmp_path)]) == 1
    assert (tmp_path / "portfolio-2" / "year-end-2026.html").exists()


def test_fifo_multiple_parcels_and_reverse_split_same_day_buy():
    h = Holding(
        "NZX",
        "FERN",
        "NZD",
        (
            trade("2025-04-01", "buy", "4", "10", "4"),
            trade("2025-04-02", "buy", "6", "20", "6"),
            # Consolidate ten units to five, then add one. Sale consumes 2+1 old units.
            trade("2025-05-01", "adjustment", "-5", "0"),
            trade("2025-05-01", "buy", "1", "30"),
            trade("2025-05-01", "sell", "3", "40", "2"),
        ),
        (Observation(day("2026-03-31"), D(50)),),
        splits=(SplitEvent(day("2025-05-01"), D(2), D(1)),),
    )
    r = calculate_year_end((h,), (), {}, 2026)
    # Oldest two post-split shares cost 44, next one costs 42. Net proceeds 118.
    assert r.total.gains == D(32)
    assert r.performance.total.closing == D(150)
