"""Offline return attribution, report rendering, previews and delivery orchestration."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from pydantic import SecretStr

from portfolio_digest import cli, report_service
from portfolio_digest.mailers import MailError, MailSettings
from portfolio_digest.models import FxRate, Income, Instrument, Price, Split, Trade
from portfolio_digest.report_returns import HoldingReturn, PeriodEngine, ReportError
from portfolio_digest.reports import Report, ValuePoint, build_report, render
from portfolio_digest.scheduling import ScheduleConfig

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    def __init__(self, **rows):
        self.rows = rows

    def scalars(self, statement):
        return self.rows.get(statement.column_descriptions[0]["name"], [])


def instrument(**kwargs):
    return Instrument(id=1, exchange="ASX", code="DEMO", name="Demo & Co", currency="AUD", **kwargs)


def trade(day, kind, quantity, price, **kwargs):
    return Trade(
        id=1,
        instrument_id=1,
        trade_date=day,
        trade_type=kind,
        quantity=D(quantity),
        price=D(price),
        fees=D("0"),
        currency="AUD",
        fx_rate=None,
        portfolio_id=1,
        **kwargs,
    )


def price(day, close, stale=False):
    return Price(instrument_id=1, price_date=day, close=D(close), is_stale=stale)


def fx(day, rate):
    return FxRate(currency="AUD", base_currency="NZD", rate_date=day, rate=D(rate), is_stale=False)


def sample_session():
    return FakeSession(
        Instrument=[instrument()],
        Trade=[
            trade(date(2026, 10, 1), "buy", "10", "10"),
            trade(date(2026, 10, 6), "buy", "2", "11"),
            trade(date(2026, 10, 8), "sell", "3", "12"),
        ],
        Price=[price(date(2026, 10, 2), "10"), price(date(2026, 10, 9), "12")],
        FxRate=[
            fx(date(2026, 10, 2), "1.1"),
            fx(date(2026, 10, 6), "1.15"),
            fx(date(2026, 10, 8), "1.2"),
            fx(date(2026, 10, 9), "1.2"),
        ],
        Income=[
            Income(
                instrument_id=1,
                paid_date=date(2026, 10, 8),
                net_amount=D("5"),
                gross_amount=D("6"),
                currency="AUD",
                fx_rate=None,
                portfolio_id=1,
            )
        ],
    )


def test_returns_attribute_trades_fx_and_net_income_without_rounding():
    (row,) = PeriodEngine(sample_session()).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.units == D("9")
    assert row.opening_value == D("110")
    assert row.value == D("129.6")
    assert row.capital_gain == D("26.4")
    # Net NZD contributions = 22*1.15 - 36*1.2 = -17.9.
    assert row.currency_gain == D("11.1")
    assert row.dividends == D("6")
    assert row.total_return == D("43.5")
    assert row.total_return == row.value - row.opening_value + D("17.9") + D("6")


def test_sold_holdings_stay_visible_and_old_closed_holdings_drop_out():
    session = sample_session()
    session.rows["Trade"].append(trade(date(2026, 10, 9), "sell", "9", "12"))
    engine = PeriodEngine(session)
    (row,) = engine.calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.value == 0
    assert row.total_return == D("43.5")
    assert engine.calculate(date(2026, 11, 1), date(2026, 11, 7)) == ()


def test_missing_data_stops_report_instead_of_omitting_holding():
    session = sample_session()
    session.rows["Price"] = []
    with pytest.raises(ReportError, match="missing prices"):
        PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    session = sample_session()
    session.rows["FxRate"] = []
    with pytest.raises(ReportError, match="missing exchange rates"):
        PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))


def test_raw_split_prices_and_adjustment_units_do_not_create_capital_gain():
    session = FakeSession(
        Instrument=[instrument()],
        Trade=[
            trade(date(2026, 10, 1), "buy", "1", "40"),
            trade(date(2026, 10, 6), "adjustment", "3", "0"),
        ],
        Price=[price(date(2026, 10, 2), "40"), price(date(2026, 10, 9), "10")],
        FxRate=[fx(date(2026, 10, 2), "1.1"), fx(date(2026, 10, 9), "1.1")],
        Split=[
            Split(
                instrument_id=1, split_date=date(2026, 10, 6), shares_before=D(1), shares_after=D(4)
            )
        ],
    )
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.units == 4
    assert row.total_return == 0


def test_fees_reduce_return_and_opening_balance_is_a_flow():
    session = FakeSession(
        Instrument=[instrument()],
        Trade=[trade(date(2026, 10, 6), "opening_balance", "10", "10")],
        Price=[price(date(2026, 10, 9), "12")],
        FxRate=[fx(date(2026, 10, 6), "1.2"), fx(date(2026, 10, 9), "1.2")],
    )
    session.rows["Trade"][0].fees = D("2")
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.capital_gain == D("21.6")


def example_report():
    return Report(
        date(2026, 10, 5),
        date(2026, 10, 11),
        (
            HoldingReturn(
                "ASX",
                "DEMO",
                "Demo & Co",
                "AUD",
                D("20"),
                D("8.5"),
                D("187"),
                D("-5"),
                D("2"),
                D("1"),
                D("191"),
                True,
            ),
            HoldingReturn(
                "NZX",
                "ALP",
                "Alpine Example",
                "NZD",
                D("100"),
                D("12.34"),
                D("1234"),
                D("34"),
                D("7"),
                D("0"),
                D("1200"),
            ),
        ),
        (ValuePoint(date(2026, 10, 4), D("1391")), ValuePoint(date(2026, 10, 11), D("1421"))),
    )


def test_html_and_text_match_reviewable_synthetic_snapshots():
    html, text = render(example_report())
    assert html == (FIXTURES / "report.html").read_text()
    assert text == (FIXTURES / "report.txt").read_text()
    assert "NZ$ 1,421.00" in html
    assert html.count("Subtotal") == 2
    assert "(stale)" in html
    assert "Currency gain" in text


def test_html_escapes_untrusted_instrument_labels():
    report = example_report()
    bad = HoldingReturn(
        "<script>", "<img src=x>", "x", "NZD", D(1), D(1), D(1), D(0), D(0), D(0), D(1)
    )
    html, _ = render(Report(report.start, report.end, (bad,)))
    assert "<script>" not in html and "<img src=x>" not in html
    assert "&lt;script&gt;" in html


def report_config():
    return ScheduleConfig.model_validate(
        {
            "schedules": [
                {
                    "name": "weekly",
                    "kind": "report",
                    "frequency": "weekly",
                    "at": "20:00",
                    "days": 7,
                }
            ]
        }
    )


def test_cli_dry_run_writes_both_parts_without_loading_mail_settings_or_sending(
    tmp_path, monkeypatch
):
    @contextmanager
    def session_scope(_):
        yield FakeSession()

    monkeypatch.setattr(cli, "session_scope", session_scope)
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(report_service, "build_report", lambda *a: example_report())
    monkeypatch.setattr(
        report_service, "mail_settings", lambda: pytest.fail("Loaded mail credentials")
    )
    monkeypatch.setattr(report_service, "send", lambda *a: pytest.fail("Email sent in dry-run"))
    monkeypatch.setattr(report_service, "ping_kuma", lambda *a: pytest.fail("Ping in dry-run"))
    config = tmp_path / "schedules.toml"
    config.write_text(
        '[[schedules]]\nname="weekly"\nkind="report"\nfrequency="weekly"\nat="20:00"\n'
    )
    out = tmp_path / "report.html"
    assert (
        cli.main(
            [
                "report",
                "send-now",
                "--schedule",
                "weekly",
                "--config",
                str(config),
                "--dry-run",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.read_text() == render(example_report())[0]
    assert out.with_suffix(".txt").read_text() == render(example_report())[1]


def test_successful_send_uses_owner_default_and_only_then_pings(monkeypatch):
    monkeypatch.setattr(report_service, "build_report", lambda *a: example_report())
    calls = []
    monkeypatch.setattr(report_service, "send", lambda s, m, r: calls.append((m, r)))
    monkeypatch.setattr(report_service, "ping_kuma", lambda c: calls.append("ping"))
    run_settings = MailSettings(
        _env_file=None, mail_from="digest@example.invalid", mail_owner="owner@example.invalid"
    )
    report_service.run_report(FakeSession(), report_config(), "weekly", settings=run_settings)
    assert calls[0][1] == ["owner@example.invalid"]
    assert calls[0][0].is_multipart()
    assert calls[1] == "ping"


def test_failed_send_does_not_ping_and_cli_exits_nonzero(monkeypatch, capsys):
    def fail(*a):
        raise MailError("Mail delivery failed.")

    monkeypatch.setattr(cli, "cmd_report_send", fail)
    assert cli.main(["report", "send-now", "--schedule", "weekly"]) == 1
    assert "Mail delivery failed" in capsys.readouterr().err
    monkeypatch.setattr(report_service, "build_report", lambda *a: example_report())
    monkeypatch.setattr(report_service, "send", fail)
    monkeypatch.setattr(report_service, "ping_kuma", lambda *a: pytest.fail("Ping after failure"))
    with pytest.raises(MailError):
        report_service.run_report(
            FakeSession(),
            report_config(),
            "weekly",
            settings=MailSettings(
                _env_file=None, mail_from="a@example.invalid", mail_owner="b@example.invalid"
            ),
        )


def test_failed_monitoring_is_nonfatal_and_never_logs_secret_url(monkeypatch, caplog):
    config = report_config()
    config.kuma_push_url = SecretStr("https://example.invalid/private-token")

    def fail(*a, **kw):
        raise OSError("https://example.invalid/private-token")

    monkeypatch.setattr(report_service, "urlopen", fail)
    report_service.ping_kuma(config)
    assert "monitoring push failed" in caplog.text
    assert "private-token" not in caplog.text


def test_chart_uses_real_opening_and_closing_values():
    session = sample_session()
    report = build_report(PeriodEngine(session), date(2026, 10, 5), date(2026, 10, 9))
    assert report.history[0].value == D("110")
    assert report.history[-1].value == report.value
    assert report.history[0].day == date(2026, 10, 4)


def test_gift_adjustment_is_external_flow_not_return():
    session = sample_session()
    session.rows["Trade"] = [trade(date(2026, 10, 6), "adjustment", "10", "0")]
    session.rows["Price"].insert(1, price(date(2026, 10, 6), "11"))
    session.rows["Income"] = []
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.value == D("144")
    assert row.capital_gain == D("12")
    assert row.currency_gain == D("5.5")
    assert row.total_return == D("17.5")


def test_broker_fx_difference_is_currency_gain_not_capital():
    session = sample_session()
    session.rows["Trade"] = [trade(date(2026, 10, 6), "buy", "10", "11")]
    session.rows["Trade"][0].fx_rate = D("1.1")
    session.rows["Income"] = []
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.capital_gain == D("12")
    assert row.currency_gain == D("11")
    assert row.total_return == D("23")


def test_drp_adjustment_does_not_double_count_dividend_return():
    session = sample_session()
    session.rows["Trade"] = [
        trade(date(2026, 10, 1), "buy", "10", "10"),
        trade(date(2026, 10, 8), "adjustment", "1", "0"),
    ]
    session.rows["Income"] = [
        Income(
            id=1,
            portfolio_id=1,
            instrument_id=1,
            paid_date=date(2026, 10, 8),
            currency="AUD",
            net_amount=D("12"),
            drp_quantity=D("1"),
            fx_rate=None,
        )
    ]
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.value == D("158.4")
    assert row.dividends == D("14.4")
    assert row.total_return == D("48.4")  # closing value less opening, no external cash flows


def test_stale_fx_and_stale_price_are_flagged_without_omitting_value():
    session = sample_session()
    session.rows["Price"][-1].is_stale = True
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.stale
    assert row.value == D("129.6")
    session.rows["Price"][-1].is_stale = False
    session.rows["FxRate"][-1].is_stale = True
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.stale


def test_broker_rates_in_foreign_portfolio_base_are_converted_to_nzd():
    from portfolio_digest.models import Portfolio

    session = sample_session()
    session.rows["Portfolio"] = [
        Portfolio(id=1, name="Synthetic AUD portfolio", base_currency="AUD")
    ]
    session.rows["Trade"] = [trade(date(2026, 10, 6), "buy", "10", "11")]
    session.rows["Trade"][0].fx_rate = D("1")  # AUD to portfolio AUD, not AUD to NZD
    session.rows["Income"] = []
    (row,) = PeriodEngine(session).calculate(date(2026, 10, 5), date(2026, 10, 9))
    assert row.capital_gain == D("12")
    assert row.currency_gain == D("5.5")
    assert row.total_return == D("17.5")
