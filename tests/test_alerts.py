"""Made-up series exercise alert rules, delivery and durable suppression."""

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


def test_cooldown_repeats_only_when_elapsed(sample):
    session, _, _ = sample
    rules = config(cooldown_hours=2)
    alerts.run_alerts(session, rules, now=NOW)
    session.flush()
    session.expire_all()
    assert not alerts.run_alerts(session, rules, now=NOW + timedelta(hours=1))
    assert alerts.run_alerts(session, rules, now=NOW + timedelta(hours=2))
    assert alerts.send.call_count == 2


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

    monkeypatch.setattr(alerts, "urlopen", fail)
    alerts.push("Made-up figures")
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
    monkeypatch.setattr(alerts, "urlopen", request)
    alerts.push("Made up")
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
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(alerts, "urlopen", request)
    alerts.push("Made-up alert")
    sent = request.call_args.args[0]
    assert sent.full_url == "https://api.pushover.net/1/messages.json"
    assert parse_qs(sent.data.decode()) == {
        "token": ["invented-app-token"],
        "user": ["invented-user-key"],
        "message": ["Made-up alert"],
    }


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


@pytest.mark.parametrize("fetch_result", [0, 1])
def test_scheduler_checks_alerts_after_success_or_partial_fetch(monkeypatch, fetch_result):
    from argparse import Namespace
    from contextlib import nullcontext
    from datetime import time

    from portfolio_digest import cli
    from portfolio_digest.scheduling import Schedule

    events = []
    rules = config()
    monkeypatch.setattr(cli, "read_config", lambda _: rules)
    monkeypatch.setattr(cli, "make_engine", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda _: nullcontext("session"))
    monkeypatch.setattr(
        cli, "cmd_prices_fetch", lambda _: events.append("fetch committed") or fetch_result
    )
    monkeypatch.setattr(cli, "run_alerts", lambda s, c: events.append((s, c)))

    def run(_, execute):
        return execute(Schedule(name="prices", kind="prices", frequency="daily", at=time(18)), NOW)

    monkeypatch.setattr(cli.Scheduler, "run", run)
    assert cli.cmd_scheduler(Namespace(config="schedules.toml", state="unused")) == fetch_result
    assert events == ["fetch committed", ("session", rules)]


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
    assert session.get(AlertState, (1, "daily:NZX:MADEUP")) is not None
    assert alerts.send.call_count == 1


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
