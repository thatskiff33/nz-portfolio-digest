"""Made-up series exercise alert rules, delivery and durable suppression."""

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import Mock

import pytest
from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session

from portfolio_digest import alerts
from portfolio_digest.alert_config import AlertConfig, HoldingRules
from portfolio_digest.mailers import MailError, MailSettings
from portfolio_digest.models import AlertState, Base, Instrument, Portfolio, Price, Trade
from portfolio_digest.scheduling import ScheduleConfig

NOW = datetime(2026, 10, 16, 7, tzinfo=UTC)


@pytest.fixture
def sample(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for constraint in list(copied.constraints):
            if isinstance(constraint, CheckConstraint):
                copied.constraints.remove(constraint)
    metadata.create_all(engine)
    with Session(engine) as session:
        portfolio = Portfolio(name="Imaginary", base_currency="NZD")
        instrument = Instrument(exchange="NZX", code="MADEUP", currency="NZD", name="Made up")
        session.add_all([portfolio, instrument])
        session.flush()
        session.add(
            Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=date(2026, 10, 1),
                trade_type="buy",
                quantity=D(10),
                price=D(10),
                currency="NZD",
                fees=D(0),
                source="manual",
                external_ref="invented",
            )
        )
        for day, value in [(1, "10"), (9, "10"), (15, "10"), (16, "8")]:
            session.add(
                Price(
                    instrument_id=instrument.id,
                    price_date=date(2026, 10, day),
                    close=D(value),
                    source="made_up",
                    is_stale=False,
                )
            )
        session.commit()
        monkeypatch.setattr(
            alerts,
            "mail_settings",
            lambda: MailSettings(
                mail_from="sender@example.invalid", mail_owner="reader@example.invalid"
            ),
        )
        monkeypatch.setattr(alerts, "send", Mock())
        monkeypatch.setattr(alerts, "push", Mock())
        yield session, portfolio, instrument
    engine.dispose()


def config(**values):
    return ScheduleConfig(schedules=[], alerts=AlertConfig(**values))


def test_daily_and_portfolio_figures_from_engine_and_overrides(sample):
    session, portfolio, _ = sample
    states = alerts.evaluate(session, portfolio, config(), NOW)
    assert "-20.00%" in states["daily:NZX:MADEUP"].text
    assert "100.00 to 80.00 NZD" in states["portfolio"].text
    assert states["stale:NZX:MADEUP"] is None
    states = alerts.evaluate(
        session, portfolio, config(holdings={"NZX:MADEUP": HoldingRules(daily_percent=D(25))}), NOW
    )
    assert states["daily:NZX:MADEUP"] is None


def test_stale_uses_completed_trading_days_not_weekends(sample):
    session, portfolio, _ = sample
    sunday = datetime(2026, 10, 18, 7, tzinfo=UTC)
    assert (
        alerts.evaluate(session, portfolio, config(stale_trading_days=0), sunday)[
            "stale:NZX:MADEUP"
        ]
        is None
    )
    monday = datetime(2026, 10, 19, 7, tzinfo=UTC)
    assert (
        "1 trading days"
        in alerts.evaluate(session, portfolio, config(stale_trading_days=0), monday)[
            "stale:NZX:MADEUP"
        ].text
    )


def test_dry_run_sends_nothing_and_does_not_suppress_real_delivery(sample):
    session, _, _ = sample
    assert alerts.run_alerts(session, config(), dry_run=True, now=NOW)
    assert not session.scalars(select(AlertState)).all()
    alerts.send.assert_not_called()
    alerts.push.assert_not_called()
    assert alerts.run_alerts(session, config(), now=NOW)
    session.commit()
    assert not alerts.run_alerts(session, config(), now=NOW)
    assert alerts.send.call_count == alerts.push.call_count == 1
    text, count = alerts.push.call_args.args
    # The default config fires the daily-move and portfolio-drop rules.
    assert count == 2 and "-20.00%" in text


def test_cooldown_repeats_only_when_elapsed(sample):
    session, _, _ = sample
    rules = config(cooldown_hours=2)
    first = alerts.run_alerts(session, rules, now=NOW)
    assert "moved" in first[0]
    assert "Portfolio value changed" in first[0]
    session.flush()
    session.expire_all()
    assert not alerts.run_alerts(session, rules, now=NOW + timedelta(hours=1))
    second = alerts.run_alerts(session, rules, now=NOW + timedelta(hours=2))
    assert "Portfolio value changed" in second[0]
    assert "moved" not in second[0]
    assert alerts.send.call_count == 2


@pytest.mark.parametrize("cooldown_hours", [None, 100])
def test_consecutive_qualifying_daily_moves_both_send(sample, cooldown_hours):
    session, portfolio, instrument = sample
    session.add(
        Price(
            instrument_id=instrument.id,
            price_date=date(2026, 10, 19),
            close=D("7"),
            source="made_up",
            is_stale=False,
        )
    )
    session.commit()
    # Test the default and a cooldown longer than the gap between the two closes.
    rules = config(cooldown_hours=cooldown_hours, portfolio_drop_percent=D("99"))
    friday = alerts.run_alerts(session, rules, now=NOW)
    monday = alerts.run_alerts(session, rules, now=datetime(2026, 10, 19, 7, tzinfo=UTC))
    assert "2026-10-15 to 2026-10-16" in friday[0]
    assert "-20.00%" in friday[0]
    assert "2026-10-16 to 2026-10-19" in monday[0]
    assert "-12.50%" in monday[0]
    assert alerts.send.call_count == 2
    assert session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP:2026-10-15:2026-10-16")) is None
    assert (
        session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP:2026-10-16:2026-10-19"))
        is not None
    )


def test_same_daily_pair_does_not_resend_later_that_day_or_over_the_weekend(sample):
    session, portfolio, _ = sample
    rules = config(cooldown_hours=1, portfolio_drop_percent=D("99"))
    friday = alerts.run_alerts(session, rules, now=NOW)
    assert "2026-10-15 to 2026-10-16" in friday[0]
    alerts.send.reset_mock()
    for now in (
        NOW + timedelta(hours=2),
        datetime(2026, 10, 17, 7, tzinfo=UTC),
        datetime(2026, 10, 18, 7, tzinfo=UTC),
    ):
        assert alerts.run_alerts(session, rules, now=now) == []
    alerts.send.assert_not_called()
    assert (
        session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP:2026-10-15:2026-10-16"))
        is not None
    )


def test_cooldown_still_repeats_a_stale_price(sample):
    session, _, instrument = sample
    latest = session.scalar(
        select(Price).where(
            Price.instrument_id == instrument.id, Price.price_date == date(2026, 10, 16)
        )
    )
    session.delete(latest)
    session.commit()
    rules = config(cooldown_hours=2, stale_trading_days=0, portfolio_drop_percent=D("99"))
    first = alerts.run_alerts(session, rules, now=NOW)
    assert "price stale" in first[0]
    assert "moved" not in first[0]
    assert not alerts.run_alerts(session, rules, now=NOW + timedelta(hours=1))
    second = alerts.run_alerts(session, rules, now=NOW + timedelta(hours=2))
    assert "price stale" in second[0]
    assert alerts.send.call_count == 2


def test_under_threshold_clears_the_recorded_daily_pair(sample):
    session, portfolio, _ = sample
    rules = config(portfolio_drop_percent=D("99"))
    alerts.run_alerts(session, rules, now=NOW)
    pair = "daily:NZX:MADEUP:2026-10-15:2026-10-16"
    assert session.get(AlertState, (portfolio.id, pair)) is not None
    widened = config(daily_percent=D("25"), portfolio_drop_percent=D("99"))
    alerts.run_alerts(session, widened, now=NOW)
    assert session.get(AlertState, (portfolio.id, pair)) is None


def test_missing_close_keeps_the_recorded_daily_pair(sample):
    session, portfolio, instrument = sample
    rules = config(portfolio_drop_percent=D("99"))
    alerts.run_alerts(session, rules, now=NOW)
    price = session.scalar(
        select(Price).where(
            Price.instrument_id == instrument.id, Price.price_date == date(2026, 10, 16)
        )
    )
    price.is_stale = True
    session.commit()
    assert not alerts.run_alerts(session, rules, now=NOW)
    assert (
        session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP:2026-10-15:2026-10-16"))
        is not None
    )


def test_undated_daily_row_does_not_suppress_the_current_pair(sample):
    session, portfolio, _ = sample
    session.add(AlertState(portfolio_id=portfolio.id, rule_key="daily:NZX:MADEUP", sent_at=NOW))
    session.commit()
    rules = config(portfolio_drop_percent=D("99"))
    sent = alerts.run_alerts(session, rules, now=NOW)
    assert "2026-10-15 to 2026-10-16" in sent[0]
    assert session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP")) is None
    assert (
        session.get(AlertState, (portfolio.id, "daily:NZX:MADEUP:2026-10-15:2026-10-16"))
        is not None
    )


def test_clear_rearms_rule_and_unknown_data_does_not_clear(sample, monkeypatch):
    session, _, _ = sample
    key = "daily:NZX:MADEUP"
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {key: alerts.Trigger(key, "Made up move")})
    alerts.run_alerts(session, config(), now=NOW)
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {})
    assert not alerts.run_alerts(session, config(), now=NOW)
    assert session.scalars(select(AlertState)).all()
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {key: None})
    alerts.run_alerts(session, config(), now=NOW)
    session.flush()
    assert not session.scalars(select(AlertState)).all()
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {key: alerts.Trigger(key, "Made up move")})
    assert alerts.run_alerts(session, config(), now=NOW)


def test_mail_failure_does_not_record_delivery(sample):
    session, _, _ = sample
    alerts.send.side_effect = MailError("Delivery failed")
    with pytest.raises(MailError):
        alerts.run_alerts(session, config(), now=NOW)
    assert not session.scalars(select(AlertState)).all()
    alerts.push.assert_not_called()


def test_push_failure_never_logs_secret(monkeypatch, caplog):
    monkeypatch.setenv("ALERT_NTFY_URL", "https://ntfy.sh/invented-private-token")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "invented-bearer-token")
    calls = []

    def fail(request, **kwargs):
        calls.append(request)
        raise OSError("invented-private-token invented-bearer-token")

    monkeypatch.setattr(alerts._push_opener, "open", fail)
    alerts.push("Made-up figures", 1)
    assert len(calls) == 1
    assert calls[0].get_header("Authorization") == "Bearer invented-bearer-token"
    assert "invented-private-token" not in caplog.text
    assert "invented-bearer-token" not in caplog.text


def test_push_off_by_default(monkeypatch):
    for name in (
        "ALERT_NTFY_URL",
        "ALERT_NTFY_TOKEN",
        "ALERT_PUSHOVER_TOKEN",
        "ALERT_PUSHOVER_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    request = Mock()
    monkeypatch.setattr(alerts._push_opener, "open", request)
    alerts.push("Made up", 1)
    request.assert_not_called()


def test_partial_override_inherits_global_stale_threshold(sample):
    session, portfolio, _ = sample
    monday = datetime(2026, 10, 19, 7, tzinfo=UTC)
    states = alerts.evaluate(
        session,
        portfolio,
        config(stale_trading_days=0, holdings={"NZX:MADEUP": HoldingRules(daily_percent=D(25))}),
        monday,
    )
    assert states["stale:NZX:MADEUP"] is not None


def test_daily_movement_ignores_trading_costs_and_adjusts_splits(sample):
    from portfolio_digest.models import Split

    session, portfolio, instrument = sample
    session.add(
        Trade(
            portfolio_id=portfolio.id,
            instrument_id=instrument.id,
            trade_date=date(2026, 10, 16),
            trade_type="buy",
            quantity=D(1),
            price=D(8),
            currency="NZD",
            fees=D(100),
            source="manual",
            external_ref="invented-costs",
        )
    )
    assert "-20.00%" in alerts.evaluate(session, portfolio, config(), NOW)["daily:NZX:MADEUP"].text
    session.add(
        Split(
            instrument_id=instrument.id,
            split_date=date(2026, 10, 16),
            shares_before=D(1),
            shares_after=D(2),
            source="registry",
        )
    )
    assert "+60.00%" in alerts.evaluate(session, portfolio, config(), NOW)["daily:NZX:MADEUP"].text


def test_split_adjusted_repeating_price_uses_shared_formatter(sample):
    from decimal import localcontext

    from portfolio_digest.formatting import display_price
    from portfolio_digest.models import Split

    session, portfolio, instrument = sample
    session.add(
        Split(
            instrument_id=instrument.id,
            split_date=date(2026, 10, 16),
            shares_before=D(1),
            shares_after=D(7),
            source="registry",
        )
    )
    with localcontext() as context:
        context.prec = 80
        opening = D(10) * D(1) / D(7)
        closing = D(8)
        movement = (closing / opening - 1) * D(100)
        text = alerts.evaluate(session, portfolio, config(), NOW)["daily:NZX:MADEUP"].text
    assert text == (
        f"NZX:MADEUP moved {movement:+.2f}% (2026-10-15 to 2026-10-16); "
        f"closing prices {display_price(opening)} to {display_price(closing)} NZD "
        "(split-adjusted)."
    )
    assert f"{movement:+.2f}%" == "+460.00%"
    assert display_price(opening) == "1.4286"
    assert str(opening) not in text
    assert len(str(opening)) >= 80


def test_threshold_is_strict_and_missing_close_is_unknown(sample):
    session, portfolio, instrument = sample
    assert (
        alerts.evaluate(session, portfolio, config(daily_percent=D(20)), NOW)["daily:NZX:MADEUP"]
        is None
    )
    price = session.scalar(
        select(Price).where(Price.instrument_id == instrument.id, Price.price_date == NOW.date())
    )
    price.is_stale = True
    assert "daily:NZX:MADEUP" not in alerts.evaluate(session, portfolio, config(), NOW)


def test_disabled_alerts_do_not_send(sample):
    session, _, _ = sample
    assert alerts.run_alerts(session, config(enabled=False), now=NOW) == []
    alerts.send.assert_not_called()
    alerts.push.assert_not_called()


def test_pushover_request_contains_env_credentials_only_in_body(monkeypatch):
    from urllib.parse import parse_qs

    monkeypatch.delenv("ALERT_NTFY_URL", raising=False)
    monkeypatch.setenv("ALERT_PUSHOVER_TOKEN", "invented-app-token")
    monkeypatch.setenv("ALERT_PUSHOVER_USER", "invented-user-key")
    monkeypatch.setenv("ALERT_PUSH_DETAIL", "full")
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(alerts._push_opener, "open", request)
    alerts.push("Made-up alert", 1)
    sent = request.call_args.args[0]
    assert sent.full_url == "https://api.pushover.net/1/messages.json"
    assert parse_qs(sent.data.decode()) == {
        "token": ["invented-app-token"],
        "user": ["invented-user-key"],
        "message": ["Made-up alert"],
    }


FULL_ALERT = (
    "Portfolio alerts | Invented Portfolio\n\n"
    "NZX:MADEUP moved -20.00% (100.00 to 80.00 NZD)\n"
    "Portfolio fell 12.50% to 7,000.00 NZD"
)


def _captured_push(monkeypatch):
    for name in ("ALERT_NTFY_URL", "ALERT_NTFY_TOKEN", "ALERT_PUSH_DETAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALERT_PUSHOVER_TOKEN", "invented-app-token")
    monkeypatch.setenv("ALERT_PUSHOVER_USER", "invented-user-key")
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(alerts._push_opener, "open", request)
    return request


def _pushed_message(request) -> str:
    from urllib.parse import parse_qs

    return parse_qs(request.call_args.args[0].data.decode())["message"][0]


@pytest.mark.parametrize("count", [1, 2])
def test_push_is_minimal_by_default(monkeypatch, count):
    request = _captured_push(monkeypatch)
    alerts.push(FULL_ALERT, count)
    sent = _pushed_message(request)
    assert re.findall(r"\d+", sent) == [str(count)]
    for private in ("Invented Portfolio", "NZX", "MADEUP", "NZD", "%"):
        assert private not in sent
    assert "Open your email or the dashboard for details." in sent


def test_full_push_detail_sends_the_alert_text(monkeypatch):
    request = _captured_push(monkeypatch)
    monkeypatch.setenv("ALERT_PUSH_DETAIL", "full")
    alerts.push(FULL_ALERT, 2)
    assert _pushed_message(request) == FULL_ALERT


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("detail", [None, "minimal", "full"])
def test_operations_notice_push_respects_detail(monkeypatch, recovered, detail):
    from portfolio_digest import operations
    from portfolio_digest.mailers import MailSettings

    request = _captured_push(monkeypatch)
    if detail is not None:
        monkeypatch.setenv("ALERT_PUSH_DETAIL", detail)
    monkeypatch.setattr(
        operations,
        "mail_settings",
        lambda: MailSettings(
            _env_file=None,
            mail_from="sender@example.invalid",
            mail_owner="reader@example.invalid",
        ),
    )
    send = Mock()
    monkeypatch.setattr(operations, "send", send)
    when = datetime(2026, 11, 6, 7, tzinfo=UTC)
    text = (
        operations.recovery_text("prices", when, "Pacific/Auckland")
        if recovered
        else operations.failure_text("prices", when, "Pacific/Auckland", ("NZX:MADEUP",))
    )
    operations.deliver_operations_notice(ScheduleConfig(schedules=[]), text, recovered=recovered)
    email = send.call_args.args[1]
    assert email.get_body(preferencelist=("plain",)).get_content().strip() == text
    sent = _pushed_message(request)
    if detail == "full":
        assert sent == text
    else:
        expected = (
            "A portfolio job recovered. Check your email for details."
            if recovered
            else "A portfolio job failed. Check your email for details."
        )
        assert sent == expected
        assert "NZX:MADEUP" not in sent
        assert "prices" not in sent


def test_unknown_push_detail_sends_nothing(monkeypatch, caplog):
    request = _captured_push(monkeypatch)
    monkeypatch.setenv("ALERT_PUSH_DETAIL", "everything")
    alerts.push(FULL_ALERT, 2)
    request.assert_not_called()
    assert "Invalid alert push settings." in caplog.text


def test_cli_dry_run_prints_without_delivering(sample, monkeypatch, capsys):
    from contextlib import nullcontext

    from portfolio_digest import cli

    session, _, _ = sample
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext(session))
    monkeypatch.setattr(cli, "read_config", lambda _: config())
    run = alerts.run_alerts
    monkeypatch.setattr(cli, "run_alerts", lambda *a, **kw: run(*a, **kw, now=NOW))
    assert cli.main(["alerts", "check", "--dry-run"]) == 0
    assert "-20.00%" in capsys.readouterr().out
    alerts.send.assert_not_called()
    alerts.push.assert_not_called()
    assert not session.scalars(select(AlertState)).all()


@pytest.mark.parametrize(
    ("errors", "fetch_result"),
    [
        ([None], 0),
        ([None, "failed"], 0),
        (["failed"], 1),
    ],
)
def test_scheduler_checks_alerts_after_success_or_partial_fetch(monkeypatch, errors, fetch_result):
    from argparse import Namespace
    from contextlib import nullcontext
    from datetime import time

    from portfolio_digest import cli
    from portfolio_digest.prices.fetch import FetchReport, Outcome
    from portfolio_digest.scheduling import Schedule

    instruments = [Outcome("invented", error=error) for error in errors]
    report = FetchReport(outcomes=instruments, instrument_outcomes=instruments)
    checkpoint = object()
    events = []
    rules = config()
    monkeypatch.setattr(cli, "read_config", lambda _: rules)
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext("session"))
    monkeypatch.setattr(cli, "_fetch_prices", lambda _: events.append("fetch committed") or report)
    monkeypatch.setattr(
        cli, "run_alerts", lambda s, c, **kw: events.append((s, c, kw["checkpoint"]))
    )

    def run(_, execute):
        code, labels = execute(
            Schedule(name="prices", kind="prices", frequency="daily", at=time(18)), NOW, checkpoint
        )
        assert labels == tuple(outcome.label for outcome in report.outcomes if outcome.error)
        return code

    monkeypatch.setattr(cli.Scheduler, "run", run)
    assert cli.cmd_scheduler(Namespace(config="schedules.toml", state="unused")) == fetch_result
    assert events == ["fetch committed", ("session", rules, checkpoint)]


def test_suppressed_portfolio_is_logged_and_listed_on_dry_run(sample, monkeypatch, caplog, capsys):
    from contextlib import nullcontext

    from portfolio_digest import cli

    session, _, instrument = sample
    price = session.scalar(
        select(Price).where(
            Price.instrument_id == instrument.id, Price.price_date == date(2026, 10, 16)
        )
    )
    price.is_stale = True
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext(session))
    monkeypatch.setattr(cli, "read_config", lambda _: config())
    run = alerts.run_alerts
    monkeypatch.setattr(cli, "run_alerts", lambda *a, **kw: run(*a, **kw, now=NOW))
    with caplog.at_level("WARNING", logger="portfolio_digest.alerts"):
        assert cli.main(["alerts", "check", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Suppressed rules | Imaginary" in out
    assert "portfolio: valuation is incomplete or stale" in out
    assert any(
        record.message == "Alert rule skipped (portfolio): valuation is incomplete or stale."
        for record in caplog.records
    )
    suppressed = [
        line
        for line in out.splitlines()
        if line.startswith("Suppressed") or line.startswith("portfolio:")
    ]
    assert suppressed
    assert not any(char.isdigit() for line in suppressed for char in line)
    alerts.send.assert_not_called()


def test_units_error_is_logged_and_listed_without_figures(sample, monkeypatch, caplog):
    from portfolio_digest.returns import ReturnError

    monkeypatch.setattr(
        alerts,
        "units_at",
        lambda *a, **k: (_ for _ in ()).throw(ReturnError("held 99 units")),
    )
    session, _, _ = sample
    with caplog.at_level("WARNING", logger="portfolio_digest.alerts"):
        output = alerts.run_alerts(session, config(), dry_run=True, now=NOW)
    assert "held 99 units" not in caplog.text
    assert (
        "Alert rule skipped (daily:NZX:MADEUP): holding units could not be calculated."
        in caplog.text
    )
    assert (
        "Alert rule skipped (stale:NZX:MADEUP): holding units could not be calculated."
        in caplog.text
    )
    text = "\n".join(output)
    assert "daily:NZX:MADEUP: holding units could not be calculated" in text
    assert "stale:NZX:MADEUP: holding units could not be calculated" in text
    assert "held 99" not in text


def test_return_error_suppresses_portfolio_without_logging_figures(sample, monkeypatch, caplog):
    from portfolio_digest.returns import ReturnError

    monkeypatch.setattr(
        alerts,
        "load_returns",
        lambda *a, **k: (_ for _ in ()).throw(ReturnError("missing price 123.45")),
    )
    session, _, _ = sample
    with caplog.at_level("WARNING", logger="portfolio_digest.alerts"):
        output = alerts.run_alerts(session, config(), dry_run=True, now=NOW)
    assert "123.45" not in caplog.text
    assert "Alert rule skipped (portfolio): valuation could not be calculated." in caplog.text
    text = "\n".join(output)
    assert "portfolio: valuation could not be calculated" in text
    assert "123.45" not in text


def test_price_monitor_pings_only_after_a_clean_fetch(monkeypatch):
    from argparse import Namespace
    from contextlib import nullcontext
    from datetime import time

    from pydantic import SecretStr

    from portfolio_digest import cli
    from portfolio_digest.scheduling import Schedule

    url = SecretStr("https://monitor.example.invalid/prices-private")
    rules = config()
    rules.prices_kuma_push_url = url
    pings = []
    monkeypatch.setattr(cli, "read_config", lambda _: rules)
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext("session"))
    monkeypatch.setattr(cli, "run_alerts", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "ping_monitor", pings.append)
    from portfolio_digest.prices.fetch import FetchReport, Outcome

    failed = Outcome("NZX:MADEUP", error="failed")
    clean = Outcome("NZX:MADEUP")
    outcomes = [
        FetchReport(outcomes=[failed], instrument_outcomes=[failed]),
        FetchReport(outcomes=[clean], instrument_outcomes=[clean]),
    ]

    def fetch(*_args, **_kwargs):
        return outcomes.pop(0)

    monkeypatch.setattr(cli, "_fetch_prices", fetch)

    def run(_, execute):
        return execute(
            Schedule(name="prices", kind="prices", frequency="daily", at=time(10, 30)), NOW, None
        )

    monkeypatch.setattr(cli.Scheduler, "run", run)
    assert cli.cmd_scheduler(Namespace(config="schedules.toml", state="unused")) == (
        1,
        ("NZX:MADEUP",),
    )
    assert pings == []
    assert cli.cmd_scheduler(Namespace(config="schedules.toml", state="unused")) == (0, ())
    assert pings == [url]


def test_portfolio_threshold_is_strict(sample):
    session, portfolio, _ = sample
    assert (
        alerts.evaluate(session, portfolio, config(portfolio_drop_percent=D(20)), NOW)["portfolio"]
        is None
    )
    assert (
        alerts.evaluate(session, portfolio, config(portfolio_drop_percent=D(19)), NOW)["portfolio"]
        is not None
    )


def test_dry_run_does_not_clear_sent_state(sample, monkeypatch):
    session, _, _ = sample
    alerts.run_alerts(session, config(), now=NOW)
    session.commit()
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {"daily:NZX:MADEUP": None})
    assert not alerts.run_alerts(session, config(), dry_run=True, now=NOW)
    assert session.get(AlertState, (1, "daily:NZX:MADEUP:2026-10-15:2026-10-16")) is not None
    assert alerts.send.call_count == 1


@pytest.mark.parametrize("missing_day", [15, 16])
def test_missing_daily_close_is_logged_and_listed_in_dry_run(sample, caplog, missing_day):
    session, _, instrument = sample
    price = session.scalar(
        select(Price).where(
            Price.instrument_id == instrument.id,
            Price.price_date == date(2026, 10, missing_day),
        )
    )
    session.delete(price)
    session.flush()
    with caplog.at_level("WARNING", logger="portfolio_digest.alerts"):
        output = alerts.run_alerts(session, config(), dry_run=True, now=NOW)
    reason = "adjacent closing prices are missing, stale or not positive"
    assert f"Alert rule skipped (daily:NZX:MADEUP): {reason}." in caplog.text
    assert f"daily:NZX:MADEUP: {reason}" in "\n".join(output)
    assert alerts.send.call_count == 0


def test_no_price_counts_staleness_from_first_trade(sample):
    session, portfolio, instrument = sample
    for price in session.scalars(select(Price).where(Price.instrument_id == instrument.id)):
        session.delete(price)
    states = alerts.evaluate(session, portfolio, config(), NOW)
    assert "no fresh price since first trade 2026-10-01" in states["stale:NZX:MADEUP"].text
    assert "daily:NZX:MADEUP" not in states
    assert "portfolio" not in states


def test_sold_holding_clears_both_holding_rules(sample):
    session, portfolio, instrument = sample
    session.add(
        Trade(
            portfolio_id=portfolio.id,
            instrument_id=instrument.id,
            trade_date=date(2026, 10, 16),
            trade_type="sell",
            quantity=D(10),
            price=D(8),
            currency="NZD",
            fees=D(0),
            source="manual",
            external_ref="invented-sale",
        )
    )
    states = alerts.evaluate(session, portfolio, config(), NOW)
    assert states["daily:NZX:MADEUP"] is None
    assert states["stale:NZX:MADEUP"] is None


@pytest.mark.parametrize(
    ("exchange", "currency", "now"),
    [
        ("NZX", "NZD", datetime(2026, 10, 15, 21, 30, tzinfo=UTC)),
        ("NYSE", "NZD", NOW),
        ("NZX", "USD", NOW),
    ],
)
def test_portfolio_uses_completed_market_and_fx_date(sample, exchange, currency, now):
    from portfolio_digest.models import FxRate

    session, portfolio, instrument = sample
    instrument.exchange = exchange
    instrument.currency = currency
    trade = session.scalar(select(Trade))
    trade.currency = currency
    for price in session.scalars(select(Price)):
        session.delete(price)
    for day, close in [(1, "10"), (8, "10"), (15, "8")]:
        session.add(
            Price(
                instrument_id=instrument.id,
                price_date=date(2026, 10, day),
                close=D(close),
                source="made_up",
                is_stale=False,
            )
        )
        if currency != "NZD":
            session.add(
                FxRate(
                    currency=currency,
                    base_currency="NZD",
                    rate_date=date(2026, 10, day),
                    rate=D(1),
                    source="made_up",
                    is_stale=False,
                )
            )
    states = alerts.evaluate(session, portfolio, config(), now)
    assert "-20.00%" in states["portfolio"].text
    assert "2026-10-08 to 2026-10-15" in states["portfolio"].text


@pytest.mark.parametrize("target", ["http://other.example/topic", "https://other.example/topic"])
def test_push_refuses_redirects_with_credentials(target):
    from urllib.error import HTTPError
    from urllib.request import Request

    request = Request(
        "https://ntfy.example/private-topic",
        data=b"Made-up figures",
        headers={"Authorization": "Bearer invented-token"},
    )
    with pytest.raises(HTTPError):
        alerts._push_opener.error("http", request, Mock(), 302, "Found", {"location": target})


def test_unrecorded_downloaded_split_suppresses_portfolio_drop(sample):
    from portfolio_digest.models import Split

    session, portfolio, instrument = sample
    session.add(
        Split(
            instrument_id=instrument.id,
            split_date=date(2026, 10, 16),
            shares_before=D(1),
            shares_after=D(4),
            source="yahoo",
        )
    )
    session.get(Price, (instrument.id, date(2026, 10, 16))).close = D("2.5")
    states = alerts.evaluate(session, portfolio, config(), NOW)
    assert states.get("portfolio") is None


def test_failed_portfolio_does_not_roll_back_earlier_or_block_later(sample, monkeypatch):
    session, first, _ = sample
    session.add_all(
        [
            Portfolio(name="Second invented", base_currency="NZD"),
            Portfolio(name="Third invented", base_currency="NZD"),
        ]
    )
    session.commit()
    key = "invented"
    monkeypatch.setattr(alerts, "evaluate", lambda *a: {key: alerts.Trigger(key, "Invented move")})
    sent = []

    def send(settings, msg, recipients):
        text = msg.get_body(preferencelist=("plain",)).get_content()
        if "Second invented" in text:
            raise MailError("invented-private-token")
        sent.append(text)

    monkeypatch.setattr(alerts, "send", send)
    for _ in range(3):
        with pytest.raises(MailError, match="Some portfolio alerts failed") as error:
            alerts.run_alerts(session, config(), now=NOW)
        assert "invented-private-token" not in str(error.value)
        session.rollback()  # Match the CLI's failed outer session scope.
    assert len(sent) == 2
    assert len(session.scalars(select(AlertState)).all()) == 2
    assert session.get(AlertState, (2, key)) is None


def test_alert_checkpoint_skips_completed_portfolios_even_after_cooldown(sample, monkeypatch):
    from portfolio_digest.scheduling import DeliveryCheckpoint

    session, _, _ = sample
    session.add(Portfolio(name="Second invented", base_currency="NZD"))
    session.commit()
    checked = []
    saved = []
    checkpoint = DeliveryCheckpoint(set(), lambda: saved.append(set(checkpoint.delivered)))

    def evaluate(session, portfolio, config, now, skipped=None):
        checked.append(portfolio.id)
        if portfolio.id == 2:
            raise RuntimeError("invented-private-token")
        return {"invented": alerts.Trigger("invented", "Invented move")}

    monkeypatch.setattr(alerts, "evaluate", evaluate)
    for now in [NOW, NOW + timedelta(hours=8)]:
        with pytest.raises(MailError):
            alerts.run_alerts(session, config(cooldown_hours=1), now=now, checkpoint=checkpoint)
    assert checked == [1, 2, 2]
    assert saved == [{1}]
    assert alerts.send.call_count == 1


def test_alert_checkpoint_failure_stops_before_next_portfolio(sample, monkeypatch):
    from portfolio_digest.scheduling import DeliveryCheckpoint, SchedulerStateError

    session, _, _ = sample
    session.add(Portfolio(name="Second invented", base_currency="NZD"))
    session.commit()
    checked = []

    def evaluate(session, portfolio, config, now, skipped=None):
        checked.append(portfolio.id)
        return {"invented": alerts.Trigger("invented", "Invented move")}

    def fail():
        raise SchedulerStateError("Cannot write scheduler state.")

    monkeypatch.setattr(alerts, "evaluate", evaluate)
    with pytest.raises(SchedulerStateError):
        alerts.run_alerts(session, config(), now=NOW, checkpoint=DeliveryCheckpoint(set(), fail))
    assert checked == [1]
    session.rollback()
    assert session.get(AlertState, (1, "invented")) is not None
    alerts.push.assert_not_called()


def test_dry_run_neither_saves_checkpoint_nor_commits(sample, monkeypatch):
    from portfolio_digest.scheduling import DeliveryCheckpoint

    session, _, _ = sample
    checkpoint = DeliveryCheckpoint(set(), Mock())
    commit = Mock(side_effect=AssertionError("Dry run committed"))
    monkeypatch.setattr(session, "commit", commit)
    assert alerts.run_alerts(session, config(), dry_run=True, now=NOW, checkpoint=checkpoint)
    assert checkpoint.delivered == set()
    checkpoint.save.assert_not_called()
    commit.assert_not_called()


@pytest.mark.parametrize("partial", [False, True])
def test_cli_partial_fetch_advances_but_total_failure_remains_pending(
    tmp_path, monkeypatch, partial
):
    import json
    from argparse import Namespace
    from contextlib import nullcontext

    from portfolio_digest import cli
    from portfolio_digest.prices.fetch import FetchReport, Outcome
    from portfolio_digest.scheduling import Schedule, Scheduler

    schedule = Schedule(name="prices", kind="prices", frequency="daily", at="09:00")
    rules = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "prices": NOW.isoformat(),
                "prices:config": schedule.model_dump_json() + rules.timezone,
            }
        )
    )
    instruments = [Outcome("invented", error="failed")]
    if partial:
        instruments.append(Outcome("another invented"))
    report = FetchReport(outcomes=instruments, instrument_outcomes=instruments)
    monkeypatch.setattr(cli, "read_config", lambda _: rules)
    monkeypatch.setattr(cli, "_fetch_prices", lambda _: report)
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext(None))
    checkpoints = []
    monkeypatch.setattr(cli, "run_alerts", lambda *a, checkpoint: checkpoints.append(checkpoint))

    class Stop(Exception):
        pass

    def stop(_):
        raise Stop

    real_run = Scheduler.run
    monkeypatch.setattr(
        Scheduler, "run", lambda self, execute: real_run(self, execute, now=lambda: NOW, sleep=stop)
    )
    with pytest.raises(Stop):
        cli.cmd_scheduler(Namespace(config="unused", state=str(path)))
    assert len(checkpoints) == 1
    state = json.loads(path.read_text())
    due = datetime.fromisoformat(state["prices"])
    assert (due > NOW) is partial
    assert (state["prices:retry"]["retry_at"] is None) is partial


@pytest.mark.parametrize(
    ("closing", "percent", "values"),
    [("9.1875", "-8.13%", "100.00 to 91.88 NZD"), ("9.9875", "-0.13%", "100.00 to 99.88 NZD")],
)
def test_alert_percentages_round_half_up_like_other_channels(sample, closing, percent, values):
    session, portfolio, instrument = sample
    price = session.scalar(
        select(Price).where(
            Price.instrument_id == instrument.id, Price.price_date == date(2026, 10, 16)
        )
    )
    price.close = D(closing)
    session.commit()
    states = alerts.evaluate(
        session, portfolio, config(daily_percent=D("0.1"), portfolio_drop_percent=D("0.1")), NOW
    )
    assert f"moved {percent} " in states["daily:NZX:MADEUP"].text
    assert f"changed {percent} " in states["portfolio"].text
    assert values in states["portfolio"].text


def test_postmark_partial_failure_keeps_progress_after_alert_rollback(sample, monkeypatch):
    import json

    import httpx

    from portfolio_digest import mailers
    from portfolio_digest.delivery import RecipientProgress, track_recipients

    session, _, _ = sample
    recipients = ["first@example.invalid", "second@example.invalid"]
    settings = MailSettings(
        _env_file=None,
        mailer="postmark",
        mail_from="sender@example.invalid",
        postmark_server_token="test-only-token",  # noqa: S106 - fictitious sentinel
    )
    monkeypatch.setattr(alerts, "mail_settings", lambda: settings)
    monkeypatch.setattr(alerts, "send", mailers.send)
    attempted = []
    rejecting = True

    def handle(request):
        recipient = json.loads(request.content)["To"]
        attempted.append(recipient)
        code = 406 if rejecting and recipient == recipients[1] else 0
        return httpx.Response(200, json={"ErrorCode": code})

    factory = httpx.Client
    monkeypatch.setattr(
        mailers.httpx, "Client", lambda **kw: factory(transport=httpx.MockTransport(handle))
    )
    rules = config()
    rules.recipients = recipients
    saved = []
    progress = RecipientProgress(set(), lambda: saved.append(sorted(progress.delivered)))
    with (
        track_recipients(progress),
        pytest.raises(MailError, match="Some portfolio alerts failed"),
    ):
        alerts.run_alerts(session, rules, now=NOW)
    assert not session.scalars(select(AlertState)).all()
    assert len(saved) == 1
    session.rollback()
    rejecting = False
    # Recreate progress as on restart, without retaining an EmailMessage or adapter.
    with track_recipients(RecipientProgress(set(saved[-1]), lambda: None)):
        assert alerts.run_alerts(session, rules, now=NOW + timedelta(minutes=30))
    assert attempted == recipients + [recipients[1]]
    assert session.scalars(select(AlertState)).all()
    alerts.push.assert_called_once()
