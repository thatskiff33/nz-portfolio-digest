"""Hand-worked, entirely fictional NZ, US and AU tax-year ledger."""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from test_returns_loader import _sqlite_engine

from portfolio_digest.cli import main
from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import MailSettings
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.returns import Holding, Observation, ReturnError, SplitEvent, Transaction
from portfolio_digest.scheduling import ScheduleConfig, read_config
from portfolio_digest.year_end import Dividend, calculate_year_end
from portfolio_digest.year_end_report import render_csv, render_html
from portfolio_digest.year_end_service import (
    latest_ended_year,
    load_year_end,
    run_year_end,
)


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
        pytest.fail("dry-run attempted mail delivery")

    monkeypatch.setattr("portfolio_digest.report_service.send", forbidden)
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


@pytest.mark.parametrize("provider", ["gmail", "smtp", "postmark", "ses"])
@pytest.mark.parametrize("recipients", [[], ["first@example.invalid", "second@example.invalid"]])
def test_delivery_uses_shared_mailer_and_attachment(year_engine, monkeypatch, provider, recipients):
    settings = MailSettings(
        _env_file=None,
        mailer=provider,
        mail_from="digest@example.invalid",
        mail_owner="owner@example.invalid",
    )
    config = ScheduleConfig(schedules=[], recipients=recipients)
    calls = []
    monkeypatch.setattr(
        "portfolio_digest.report_service.send", lambda s, m, r: calls.append((s, m, r))
    )
    with Session(year_engine) as session:
        run_year_end(session, config, "Invented", 2026, settings=settings)
    used_settings, message, envelope = calls[0]
    assert used_settings is settings
    assert envelope == (recipients or ["owner@example.invalid"])
    assert message["To"] == "undisclosed-recipients:;"
    assert "31 March 2026" in message["Subject"]
    assert "12,638.00" in message.get_body(preferencelist=("html",)).get_content()
    assert "12,638.00" in message.get_body(preferencelist=("plain",)).get_content()
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "year-end-2026.csv"
    assert "12638" in attachment.get_payload(decode=True).decode()


def test_year_end_preview_requires_html_path(year_engine, tmp_path):
    with Session(year_engine) as session:
        with pytest.raises(ConfigError, match="--out"):
            run_year_end(session, ScheduleConfig(schedules=[]), "Invented", 2026, dry_run=True)
        with pytest.raises(ConfigError, match="--out"):
            run_year_end(
                session,
                ScheduleConfig(schedules=[]),
                "Invented",
                2026,
                dry_run=True,
                out=tmp_path / "preview.csv",
            )


def test_scheduled_year_end_uses_original_due_year_and_previews_without_credentials(
    year_engine, monkeypatch, tmp_path
):
    from portfolio_digest.report_service import run_report

    config = read_config(Path("schedules.toml"))
    monkeypatch.setattr(
        "portfolio_digest.report_service.mail_settings",
        lambda: pytest.fail("Preview loaded credentials"),
    )
    monkeypatch.setattr("portfolio_digest.report_service.send", lambda *a: pytest.fail("Sent"))
    monkeypatch.setattr("portfolio_digest.report_service.ping_kuma", lambda *a: pytest.fail("Ping"))
    with Session(year_engine) as session:
        run_report(
            session,
            config,
            "year-end",
            dry_run=True,
            out=tmp_path / "annual.html",
            now=datetime(2026, 3, 31, 20, tzinfo=UTC),
        )
    assert "31 March 2026" in (tmp_path / "annual-portfolio-1.txt").read_text()
    assert "Invented" in (tmp_path / "annual-portfolio-1.html").read_text()
    assert "12638" in (tmp_path / "annual-portfolio-1.csv").read_text()


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


@pytest.mark.parametrize("rebuy", [False, True])
def test_completed_pre_year_positions_need_no_historical_fx(rebuy):
    trades = (
        trade("2020-01-01", "buy", "10", "20"),
        trade("2021-01-01", "sell", "10", "30"),
    )
    if rebuy:
        trades += (
            trade("2025-05-01", "buy", "10", "40", fx="2"),
            trade("2025-06-01", "sell", "10", "50", fx="2"),
        )
    h = Holding("NASDAQ", "OLD", "USD", trades)
    report = calculate_year_end((h,), (), {}, 2026)
    assert report.total.gains == D(200 if rebuy else 0)
    assert report.foreign_peak == D(800 if rebuy else 0)
    assert report.flags == ()


def test_year_end_loader_retains_historical_broker_fx(monkeypatch):
    engine = _sqlite_engine(monkeypatch)
    with Session(engine) as session:
        portfolio = Portfolio(name="Historical", base_currency="NZD")
        instrument = Instrument(exchange="NASDAQ", code="OLD", name="Invented", currency="USD")
        session.add_all([portfolio, instrument])
        session.flush()
        for when, kind, price, fx in [
            ("2024-05-01", "buy", "20", "1.5"),
            ("2025-05-01", "sell", "30", "2"),
        ]:
            session.add(
                Trade(
                    portfolio_id=portfolio.id,
                    instrument_id=instrument.id,
                    trade_date=day(when),
                    trade_type=kind,
                    quantity=D(10),
                    price=D(price),
                    currency="USD",
                    fx_rate=D(fx),
                    source="invented",
                    external_ref=kind,
                )
            )
        session.add_all(
            [
                Price(
                    instrument_id=instrument.id,
                    price_date=day("2025-03-31"),
                    close=D(25),
                    source="invented",
                ),
                FxRate(
                    currency="USD",
                    base_currency="NZD",
                    rate_date=day("2025-03-31"),
                    rate=D(2),
                    source="invented",
                ),
            ]
        )
        session.commit()
        result = load_year_end(session, portfolio.name, 2026)
        # No historical market FX exists: recorded execution FX must survive loading.
        assert result.total.gains == D(300)  # 10*30*2 proceeds - 10*20*1.5 cost.
        assert result.foreign_peak == D(300)
    engine.dispose()


def test_scheduled_year_end_send_failure_exits_nonzero_without_monitoring(
    year_engine, monkeypatch, capsys
):
    from portfolio_digest.mailers import MailError

    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    monkeypatch.setattr(
        "portfolio_digest.report_service.mail_settings",
        lambda: MailSettings(
            _env_file=None, mail_from="a@example.invalid", mail_owner="b@example.invalid"
        ),
    )

    def fail(*args):
        raise MailError("Mail delivery failed.")

    monkeypatch.setattr("portfolio_digest.report_service.send", fail)
    monkeypatch.setattr("portfolio_digest.report_service.ping_kuma", lambda *a: pytest.fail("Ping"))
    monkeypatch.setattr("portfolio_digest.year_end_service.latest_ended_year", lambda *a: 2026)
    assert main(["report", "send-now", "--schedule", "year-end"]) == 1
    assert "Mail delivery failed" in capsys.readouterr().err


def test_year_end_scheduler_dispatch_retains_persisted_due_date(year_engine, monkeypatch):
    import argparse

    from portfolio_digest.cli import cmd_scheduler
    from portfolio_digest.scheduling import Scheduler

    due = datetime(2026, 3, 31, 20, tzinfo=UTC)
    seen = []

    def run(self, execute):
        return execute(self.config.named_report("year-end"), due)

    monkeypatch.setattr(Scheduler, "run", run)
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    monkeypatch.setattr(
        "portfolio_digest.cli.run_report",
        lambda session, config, name, **kwargs: seen.append((name, kwargs["now"])),
    )
    assert cmd_scheduler(argparse.Namespace(config="schedules.toml", state="unused.json")) == 0
    assert seen == [("year-end", due)]
