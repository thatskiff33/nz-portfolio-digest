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
    # SQLite drops timezone information on reload; Postgres preserves it in production.
    for state in session.scalars(select(AlertState)):
        state.sent_at = state.sent_at.replace(tzinfo=UTC)
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
