"""Hand-worked, entirely fictional NZ, US and AU tax-year ledger."""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal as D
from decimal import InvalidOperation
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from test_returns_loader import _sqlite_engine

from portfolio_digest.cli import main
from portfolio_digest.config import ConfigError
from portfolio_digest.formatting import display_number
from portfolio_digest.mailers import MailSettings
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.returns import (
    Components,
    Holding,
    Observation,
    Report,
    Result,
    ReturnError,
    SplitEvent,
    Transaction,
)
from portfolio_digest.scheduling import ScheduleConfig, read_config
from portfolio_digest.year_end import NOTES, Dividend, TaxRow, YearEnd, calculate_year_end
from portfolio_digest.year_end_report import COLUMNS, render_csv, render_html
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
    ) == (D(1200), D(1368), D(579))
    assert performance["NASDAQ:CLOUD"].amounts.total == D(12)
    assert performance["ASX:GUM"].amounts.total == D(12036)
    p = report.performance.total
    # 66,168 close + 693 sales - 1,200 open - 53,103 buys - 16 DRP + 85 net income.
    assert (p.opening, p.closing, p.purchases, p.sales, p.adjustments, p.amounts.total) == (
        D(1200),
        D(66168),
        D(53103),
        D(693),
        D(16),
        D(12627),
    )
    assert p.percentages.total.quantize(D(".01")) == D("23.25")
    assert (report.foreign_peak, report.foreign_peak_day, report.fif_exceeded) == (
        D(52800),
        day("2025-11-01"),
        True,
    )
    # These fixtures record only combined withholding, so the exchange fallback applies.
    assert "Unclassified withholding for NZX:FERN used the exchange fallback." in report.flags
    assert "Unclassified withholding for NASDAQ:CLOUD used the exchange fallback." in report.flags
    assert all("ASX:GUM" not in flag for flag in report.flags)


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
                    resident_withholding_tax=p.resident_withholding_tax,
                    foreign_withholding_tax=p.foreign_withholding_tax,
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
    assert loaded.performance.total.amounts.total == D(12627)
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
    assert "12627" in (tmp_path / "year-end-2026.csv").read_text()
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
    assert "12,627.00" in message.get_body(preferencelist=("html",)).get_content()
    assert "12,627.00" in message.get_body(preferencelist=("plain",)).get_content()
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "year-end-2026.csv"
    assert "12627" in attachment.get_payload(decode=True).decode()


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
    assert "12627" in (tmp_path / "annual-portfolio-1.csv").read_text()


def test_all_portfolios_continue_after_one_fails(year_engine, monkeypatch, tmp_path):
    with Session(year_engine) as session:
        session.add(Portfolio(name="Empty", base_currency="NZD"))
        session.query(Portfolio).filter_by(name="Invented").one().base_currency = "USD"
        session.commit()
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    assert main(["year-end", "--all", "--year", "2026", "--dry-run", "--out", str(tmp_path)]) == 1
    assert (tmp_path / "portfolio-2" / "year-end-2026.html").exists()


def test_scheduled_year_end_skips_non_nzd_portfolios(year_engine, monkeypatch):
    from portfolio_digest.report_service import run_report

    with Session(year_engine) as session:
        session.add_all(
            [
                Portfolio(name="Foreign base", base_currency="USD"),
                Portfolio(name="Later NZD", base_currency="NZD"),
            ]
        )
        session.commit()
    delivered = []
    monitored = []
    monkeypatch.setattr(
        "portfolio_digest.report_service.deliver_report",
        lambda config, subject, *args, **kwargs: delivered.append(subject),
    )
    monkeypatch.setattr("portfolio_digest.report_service.ping_kuma", monitored.append)
    config = read_config(Path("schedules.toml"))
    with Session(year_engine) as session:
        run_report(session, config, "year-end", now=datetime(2026, 4, 1, tzinfo=UTC))
    assert delivered == [
        "Portfolio Digest: Invented, year ended 31 March 2026",
        "Portfolio Digest: Later NZD, year ended 31 March 2026",
    ]
    assert monitored == [config]


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


def test_partial_pre_year_disposal_needs_no_proceeds_fx():
    h = Holding(
        "NASDAQ",
        "OLD",
        "USD",
        (
            trade("2023-01-01", "buy", "10", "10", fx="1.5"),
            trade("2024-01-01", "sell", "5", "12"),
            trade("2025-05-01", "sell", "2", "20", fx="2"),
        ),
        (
            Observation(day("2025-03-31"), D(15)),
            Observation(day("2026-03-31"), D(25)),
        ),
    )
    rates = {
        "USD": (
            Observation(day("2025-03-31"), D(2)),
            Observation(day("2026-03-31"), D(2)),
        )
    }
    report = calculate_year_end((h,), (), rates, 2026)
    # Prior-year proceeds do not change the surviving FIFO acquisition cost.
    assert report.foreign_peak == D(75)
    assert report.total.gains == D(50)  # 2*20*2 proceeds - 2*10*1.5 cost.
    assert report.performance.total.closing == D(150)
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
    assert "Some portfolio year-end reports failed" in capsys.readouterr().err


def test_year_end_scheduler_dispatch_retains_persisted_due_date(year_engine, monkeypatch):
    import argparse

    from portfolio_digest.cli import cmd_scheduler
    from portfolio_digest.scheduling import Scheduler

    due = datetime(2026, 3, 31, 20, tzinfo=UTC)
    seen = []

    def run(self, execute):
        return execute(self.config.named_report("year-end"), due, None)

    monkeypatch.setattr(Scheduler, "run", run)
    monkeypatch.setattr("portfolio_digest.cli.make_engine", lambda: year_engine)
    monkeypatch.setattr(
        "portfolio_digest.cli.run_report",
        lambda session, config, name, **kwargs: seen.append((name, kwargs["now"])),
    )
    assert cmd_scheduler(argparse.Namespace(config="schedules.toml", state="unused.json")) == 0
    assert seen == [("year-end", due)]


def test_year_end_and_email_agree_on_net_dividend_total_return(year_engine):
    from sqlalchemy import select

    from portfolio_digest.reports import build_report

    with Session(year_engine) as session:
        # The email chart also values intermediate dates. Supply an invented
        # quote for the AU position before the final year-end observation.
        gum = session.scalar(select(Instrument).where(Instrument.code == "GUM"))
        session.add_all(
            [
                Price(
                    instrument_id=gum.id,
                    price_date=day("2025-08-01"),
                    close=D(400),
                    source="invented",
                ),
                FxRate(
                    currency="AUD",
                    base_currency="NZD",
                    rate_date=day("2025-08-01"),
                    rate=D("1.1"),
                    source="invented",
                ),
            ]
        )
        session.flush()
        annual = load_year_end(session, "Invented", 2026)
        email = build_report(session, day("2025-04-01"), day("2026-03-31"))
    assert annual.performance.start == email.returns.start == day("2025-03-31")
    assert annual.performance.end == email.returns.end == day("2026-03-31")
    assert annual.performance.total.amounts == email.returns.total.amounts
    assert annual.performance.total.percentages == email.returns.total.percentages
    assert annual.performance.total.amounts.total == email.total_return == D(12627)
    assert annual.performance.total.amounts.dividends == annual.total.net == D(85)
    assert annual.total.gross == email.returns.total.gross_dividends == D(96)
    assert annual.total.imputation == email.returns.total.imputation_gross_up == D(10)
    assert (
        annual.total.resident + annual.total.foreign == email.returns.total.withholding_tax == D(11)
    )


def _listed(exchange, code, currency, fx=None):
    return Holding(
        exchange,
        code,
        currency,
        (trade("2025-05-01", "buy", "1", "1", fx=fx),),
        (Observation(day("2026-03-31"), D(1)),),
    )


def test_withholding_uses_stored_components_then_exchange_fallback():
    fern = _listed("NZX", "FERN", "NZD")
    exact = _listed("NZX", "EXACT", "NZD")
    pines = _listed("NZX", "PINES", "NZD")
    wattle = _listed("ASX", "WATTLE", "AUD", fx="1.2")
    dividends = (
        # NZX foreign component stays foreign. It does not use the exchange rule.
        Dividend(
            "NZX:FERN",
            day("2025-06-15"),
            "NZD",
            D(10),
            D(1),
            D(4),
            D(6),
            resident_withholding_tax=D(0),
            foreign_withholding_tax=D(4),
        ),
        # Zero withholding is not a fallback, even when both components are zero.
        Dividend("NZX:FERN", day("2025-06-16"), "NZD", D(5), D(0), D(0), D(5)),
        # Mixed components, with the NZX remainder added to resident withholding.
        Dividend(
            "NZX:FERN",
            day("2025-06-17"),
            "NZD",
            D(20),
            D(0),
            D(10),
            D(10),
            resident_withholding_tax=D(3),
            foreign_withholding_tax=D(4),
        ),
        # Outside the tax year: ignored, including its unclassified withholding.
        Dividend("NZX:FERN", day("2026-04-01"), "NZD", D(99), D(0), D(99), D(0)),
        # Both components account for the whole amount, so there is no fallback.
        Dividend(
            "NZX:EXACT",
            day("2025-06-15"),
            "NZD",
            D(5),
            D(0),
            D(5),
            D(0),
            resident_withholding_tax=D(2),
            foreign_withholding_tax=D(3),
        ),
        # Entirely unclassified NZX withholding uses the resident column and is flagged.
        Dividend("NZX:PINES", day("2025-06-15"), "NZD", D(9), D(0), D(7), D(2)),
        # ASX resident withholding stays resident. The rate comes from the FX table.
        Dividend(
            "ASX:WATTLE",
            day("2025-07-01"),
            "AUD",
            D(8),
            D(0),
            D(5),
            D(3),
            resident_withholding_tax=D(5),
        ),
        # Two unclassified ASX payments share one flag. Explicit FX converts both.
        Dividend("ASX:WATTLE", day("2025-08-01"), "AUD", D(4), D(0), D(1), D(3), D("1.25")),
        Dividend(
            "ASX:WATTLE",
            day("2025-09-01"),
            "AUD",
            D(10),
            D(0),
            D(10),
            D(0),
            D(2),
            resident_withholding_tax=D(2),
            foreign_withholding_tax=D(3),
        ),
    )
    rates = {
        "AUD": (
            Observation(day("2025-07-01"), D("1.25")),
            Observation(day("2026-03-31"), D("1.25")),
        )
    }
    report = calculate_year_end((fern, exact, pines, wattle), dividends, rates, 2026)
    rows = {row.label: row for row in report.rows}
    assert (
        rows["NZX:FERN"].gross,
        rows["NZX:FERN"].imputation,
        rows["NZX:FERN"].resident,
        rows["NZX:FERN"].foreign,
        rows["NZX:FERN"].net,
    ) == (D(35), D(1), D(6), D(8), D(21))
    assert (
        rows["NZX:EXACT"].resident,
        rows["NZX:EXACT"].foreign,
        rows["NZX:EXACT"].net,
    ) == (D(2), D(3), D(0))
    assert (rows["NZX:PINES"].resident, rows["NZX:PINES"].foreign, rows["NZX:PINES"].net) == (
        D(7),
        D(0),
        D(2),
    )
    # 5 AUD at 1.25, plus 2 AUD at 2 in the resident column. Foreign is the rest.
    assert (
        rows["ASX:WATTLE"].gross,
        rows["ASX:WATTLE"].resident,
        rows["ASX:WATTLE"].foreign,
        rows["ASX:WATTLE"].net,
    ) == (D(35), D("10.25"), D("17.25"), D("7.5"))
    assert (
        report.total.gross,
        report.total.imputation,
        report.total.resident,
        report.total.foreign,
        report.total.net,
    ) == (D(84), D(1), D("25.25"), D("28.25"), D("30.5"))
    assert report.performance.total.amounts.dividends == report.total.net
    assert report.flags == tuple(
        f"Unclassified withholding for {holding} used the exchange fallback."
        for holding in ("ASX:WATTLE", "NZX:FERN", "NZX:PINES")
    )
    html = render_html(report, "Invented")
    csv_rows = list(csv.reader(io.StringIO(render_csv(report))))
    header = next(
        index for index, row in enumerate(csv_rows) if row[:2] == ["Holding", "Realised gains"]
    )
    body = []
    for row in csv_rows[header + 1 :]:
        if not row:
            break
        body.append(row)
    assert [row[0] for row in body] == [row.label for row in (*report.rows, report.total)]
    cursor = 0
    for raw, tax in zip(body, (*report.rows, report.total), strict=True):
        amounts = [getattr(tax, field) for field, _ in COLUMNS]
        assert [D(cell) for cell in raw[1:]] == amounts
        assert all("e" not in cell.lower() for cell in raw[1:])
        for value in (tax.label, *(display_number(amount) for amount in amounts)):
            found = html.find(value, cursor)
            assert found != -1, value
            cursor = found + len(value)
    for flag in report.flags:
        assert flag in html
        assert ["Note", flag] in csv_rows


def test_withholding_components_cannot_exceed_the_combined_amount():
    holding = _listed("NZX", "FERN", "NZD")

    def dividend(**components):
        return Dividend(
            "NZX:FERN",
            day("2025-06-15"),
            "NZD",
            D(10),
            D(0),
            D(4),
            D(6),
            **components,
        )

    with pytest.raises(ReturnError, match="exceed combined withholding"):
        calculate_year_end(
            (holding,),
            (dividend(resident_withholding_tax=D(3), foreign_withholding_tax=D(2)),),
            {},
            2026,
        )
    with pytest.raises(ReturnError, match="non-negative"):
        calculate_year_end((holding,), (dividend(resident_withholding_tax=D("-0.01")),), {}, 2026)
    with pytest.raises(ReturnError, match="finite"):
        calculate_year_end((holding,), (dividend(foreign_withholding_tax=D("NaN")),), {}, 2026)


def test_loader_passes_stored_withholding_components(year_engine):
    from sqlalchemy import select

    with Session(year_engine) as session:
        portfolio = session.scalar(select(Portfolio).where(Portfolio.name == "Invented"))
        fern = session.scalar(select(Instrument).where(Instrument.code == "FERN"))
        gum = session.scalar(select(Instrument).where(Instrument.code == "GUM"))
        session.add_all(
            [
                Income(
                    portfolio_id=portfolio.id,
                    instrument_id=fern.id,
                    paid_date=day("2025-12-01"),
                    currency="NZD",
                    gross_amount=D(10),
                    net_amount=D(6),
                    withholding_tax=D(4),
                    resident_withholding_tax=D(0),
                    foreign_withholding_tax=D(4),
                    source="invented",
                    external_ref="fern-foreign-component",
                ),
                Income(
                    portfolio_id=portfolio.id,
                    instrument_id=gum.id,
                    paid_date=day("2025-12-01"),
                    currency="AUD",
                    gross_amount=D(10),
                    net_amount=D(6),
                    withholding_tax=D(4),
                    resident_withholding_tax=D(4),
                    foreign_withholding_tax=D(0),
                    fx_rate=D("1.25"),
                    source="invented",
                    external_ref="gum-resident-component",
                ),
            ]
        )
        session.commit()
        loaded = load_year_end(session, "Invented", 2026)
    rows = {row.label: row for row in loaded.rows}
    # The original FERN dividend is unclassified resident withholding of 8.
    assert (rows["NZX:FERN"].resident, rows["NZX:FERN"].foreign) == (D(8), D(4))
    # 4 AUD of stored resident withholding at 1.25, not the ASX foreign column.
    assert (rows["ASX:GUM"].resident, rows["ASX:GUM"].foreign) == (D(5), D(0))
    assert "Unclassified withholding for NZX:FERN used the exchange fallback." in loaded.flags
    assert all("ASX:GUM" not in flag for flag in loaded.flags)


def test_notes_and_readme_state_withholding_drp_and_split_rules():
    readme = " ".join(Path("README.md").read_text().split())
    rules = (
        "Resident and foreign withholding follow the components stored from broker or "
        "registry evidence.",
        "Any unclassified remainder is resident withholding for an NZX holding and "
        "foreign withholding otherwise.",
        "The report names each holding where that remainder was used.",
        "Check this classification against your tax statements.",
        "This is information, not tax advice.",
        "Imported DRPs that include a DRP quantity create the linked buy.",
        "Add a purchase manually only for residual cash or a partial reinvestment.",
        "Registry-recorded splits and consolidations need no extra quantity adjustment.",
        "A Yahoo-only split needs a matching quantity adjustment.",
        "net cash dividends",
        "information, not tax advice",
        "Tax points are to confirm with a tax specialist.",
    )
    notes = " ".join(" ".join(NOTES).split())
    for rule in rules:
        assert rule in notes
        readme_rule = (
            "This summary is information, not tax advice."
            if rule == "This is information, not tax advice."
            else rule
        )
        assert readme_rule in readme
    assert "This is information, not tax advice." in notes
    assert "Splits require a matching quantity adjustment" not in notes
    assert "recorded combined withholding field" not in notes
    assert "reinvested units must also have a recorded buy" not in notes
    assert "includes gross dividends" not in readme


def test_csv_decimals_keep_full_fixed_point_precision():
    tiny = D("2.50E-77")
    scaled_zero = D("0E-86")
    priced = Result(
        "NZX:TINY",
        tiny,
        scaled_zero,
        D(100),
        D(0),
        Components(capital=tiny, dividends=scaled_zero),
        exchange="NZX",
    )
    empty = Result("NZX:NONE", D(0), D(0), D(0), D(0), Components(), exchange="NZX")
    performance = Report(
        day("2025-03-31"),
        day("2026-03-31"),
        "NZD",
        (priced, empty),
        (),
        priced,
    )
    tax = TaxRow(
        "NZX:TINY",
        gains=tiny,
        losses=scaled_zero,
        gross=tiny,
        imputation=scaled_zero,
        resident=tiny,
        foreign=scaled_zero,
        net=tiny,
    )
    report = YearEnd(
        2026,
        performance,
        (tax,),
        tax,
        scaled_zero,
        day("2026-03-31"),
        (),
    )
    rendered = render_csv(report)
    assert format(tiny, "f") in rendered
    assert format(scaled_zero, "f") in rendered
    assert "e" not in format(tiny, "f").lower()
    assert "e" not in format(scaled_zero, "f").lower()
    rows = list(csv.reader(io.StringIO(rendered)))
    peak = next(row for row in rows if row and row[0] == "Peak foreign historical cost")
    assert D(peak[1]) == scaled_zero
    percentages = [
        row[4] for row in rows if row and row[0] in {"NZX:TINY", "NZX:NONE"} and len(row) == 5
    ]
    assert "N/A" in percentages
    tiny_percentage = next(cell for cell in percentages if cell != "N/A")
    assert D(tiny_percentage) == priced.percentages.total
    for row in rows:
        for cell in row:
            try:
                parsed = D(cell)
            except InvalidOperation:
                continue
            assert "e" not in cell.lower()
            assert D(format(parsed, "f")) == parsed


def test_scheduled_delivery_checkpoints_survive_three_failed_restarts(
    year_engine, monkeypatch, tmp_path
):
    import json
    from datetime import timedelta

    from portfolio_digest import report_service, year_end_service
    from portfolio_digest.reports import ReportError
    from portfolio_digest.scheduling import ScheduleConfig, Scheduler

    with Session(year_engine) as session:
        session.add_all(
            [
                Portfolio(name="Second invented", base_currency="NZD"),
                Portfolio(name="Third invented", base_currency="NZD"),
            ]
        )
        session.commit()
    config = read_config(Path("schedules.toml"))
    config = ScheduleConfig(schedules=[config.named_report("year-end")])
    due = datetime(2026, 3, 31, 20, tzinfo=UTC)
    instant = due + timedelta(days=800)
    path = tmp_path / "state.json"
    schedule = config.schedules[0]
    path.write_text(
        json.dumps(
            {
                schedule.name: due.isoformat(),
                schedule.name + ":config": schedule.model_dump_json() + config.timezone,
            }
        )
    )
    sends = []
    pings = []
    attempts = 0
    real_run = year_end_service.run_year_end

    def send(config, subject, *a, **kwargs):
        # Exercise real report and CSV construction before the provider boundary.
        assert kwargs["csv"] is not None
        assert "2026" in kwargs["csv_name"]
        sends.append(subject)

    def annual(session, config, name, year, **kwargs):
        nonlocal attempts
        assert year == 2026
        if name == "Second invented":
            attempts += 1
            if attempts <= 3:
                raise ReportError("invented-private-token")
        return real_run(session, config, name, year, **kwargs)

    monkeypatch.setattr(year_end_service, "run_year_end", annual)
    monkeypatch.setattr(report_service, "deliver_report", send)
    monkeypatch.setattr(report_service, "ping_kuma", pings.append)

    class Stop(Exception):
        pass

    def stop(_):
        raise Stop

    def execute(schedule, at, checkpoint):
        with Session(year_engine) as session:
            report_service.run_report(session, config, schedule.name, now=at, checkpoint=checkpoint)
        return 0

    for attempt, minutes in enumerate([0, 30, 60, 120], 1):
        instant += timedelta(minutes=minutes)
        with pytest.raises(Stop):
            Scheduler(config, path).run(execute, now=lambda instant=instant: instant, sleep=stop)
        assert len(sends) == (2 if attempt <= 3 else 3)
        assert len(pings) == (0 if attempt <= 3 else 1)
    assert attempts == 4
    assert len(sends) == 3
    assert sum("Invented," in subject for subject in sends) == 1
    assert sum("Third invented," in subject for subject in sends) == 1
    assert len(pings) == 1
    assert json.loads(path.read_text())["year-end:retry"]["delivered"] == []


def test_year_end_checkpoint_failure_stops_before_next_delivery(year_engine, monkeypatch):
    from portfolio_digest import report_service, year_end_service
    from portfolio_digest.scheduling import DeliveryCheckpoint, SchedulerStateError

    with Session(year_engine) as session:
        session.add(Portfolio(name="Second invented", base_currency="NZD"))
        session.commit()
    sent = []
    monkeypatch.setattr(
        year_end_service,
        "run_year_end",
        lambda session, config, name, year, **kw: sent.append(name),
    )
    monkeypatch.setattr(report_service, "ping_kuma", lambda *a: pytest.fail("Ping"))

    def fail():
        raise SchedulerStateError("Cannot write scheduler state.")

    with Session(year_engine) as session, pytest.raises(SchedulerStateError):
        report_service.run_report(
            session,
            read_config(Path("schedules.toml")),
            "year-end",
            checkpoint=DeliveryCheckpoint(set(), fail),
        )
    assert sent == ["Invented"]


def test_year_end_preview_ignores_delivery_checkpoint(year_engine, tmp_path):
    from unittest.mock import Mock

    from portfolio_digest import report_service
    from portfolio_digest.scheduling import DeliveryCheckpoint

    checkpoint = DeliveryCheckpoint({1}, Mock())
    with Session(year_engine) as session:
        report_service.run_report(
            session,
            read_config(Path("schedules.toml")),
            "year-end",
            dry_run=True,
            out=tmp_path / "annual.html",
            now=datetime(2026, 4, 1, tzinfo=UTC),
            checkpoint=checkpoint,
        )
    assert (tmp_path / "annual-portfolio-1.csv").exists()
    checkpoint.save.assert_not_called()


def test_year_end_accessible_tables_keep_mobile_semantics(ledger):
    html = render_html(calculate_year_end(*ledger, 2026), "Example")
    assert '<th scope="col"' in html
    assert '<th scope="row"' in html
    assert "<strong>" in html and "</strong></td>" in html
    assert "display:block" not in html and "display:none" not in html
    assert "overflow-x:auto" in html and "font-size:14px" in html
