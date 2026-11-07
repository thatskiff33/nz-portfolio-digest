"""Operations notices: once a day, then one recovery, with no figures in the text."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from portfolio_digest import alerts, operations
from portfolio_digest.mailers import MailError, MailSettings
from portfolio_digest.scheduling import ScheduleConfig

WHEN = datetime(2026, 11, 6, 7, tzinfo=UTC)


def digits_outside_dates(text: str) -> str:
    return re.sub(r"\d{4}-\d{2}-\d{2}", "", text)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_standalone_failure_is_once_a_day_then_one_recovery(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(operations.sys, "platform", platform)
    if platform == "win32":
        monkeypatch.setitem(operations.sys.modules, "fcntl", None)
    monkeypatch.setenv("SCHEDULER_STATE", str(tmp_path / "scheduler.json"))
    sent = []
    monkeypatch.setattr(
        operations,
        "deliver_operations_notice",
        lambda config, text, *, recovered: sent.append(text),
    )
    config = ScheduleConfig(schedules=[])
    operations.note_outcome(config, "akahu", failed=True, now=WHEN)
    operations.note_outcome(config, "akahu", failed=True, now=WHEN + timedelta(hours=2))
    assert len(sent) == 1
    operations.note_outcome(config, "akahu", failed=True, now=WHEN + timedelta(hours=24))
    assert len(sent) == 2
    operations.note_outcome(config, "akahu", failed=False, now=WHEN + timedelta(hours=25))
    assert len(sent) == 3
    assert "recovered" in sent[-1]
    operations.note_outcome(config, "akahu", failed=False, now=WHEN + timedelta(hours=26))
    assert len(sent) == 3
    for text in sent:
        assert not re.search(r"\d", digits_outside_dates(text))


def test_missing_state_directory_is_created_and_throttle_survives_retries(tmp_path, monkeypatch):
    missing = tmp_path / "absent" / "scheduler.json"
    monkeypatch.setenv("SCHEDULER_STATE", str(missing))
    sent = []
    monkeypatch.setattr(
        operations,
        "deliver_operations_notice",
        lambda config, text, *, recovered: sent.append(text),
    )
    operations.note_outcome(ScheduleConfig(schedules=[]), "prices", failed=True, now=WHEN)
    assert len(sent) == 1
    operations.note_outcome(ScheduleConfig(schedules=[]), "prices", failed=True, now=WHEN)
    assert len(sent) == 1
    assert missing.parent.is_dir()


@pytest.mark.parametrize("detail", [None, "minimal", "full"])
def test_notice_email_failure_is_logged_and_push_still_runs(monkeypatch, caplog, detail):
    from unittest.mock import MagicMock

    for name in ("ALERT_PUSH_DETAIL", "ALERT_NTFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALERT_NTFY_URL", "https://push.example.invalid/topic")
    if detail is not None:
        monkeypatch.setenv("ALERT_PUSH_DETAIL", detail)
    response = MagicMock()
    response.__enter__.return_value.status = 200
    request = MagicMock(return_value=response)
    monkeypatch.setattr(alerts._push_opener, "open", request)
    monkeypatch.setattr(
        operations,
        "mail_settings",
        lambda: MailSettings(
            _env_file=None,
            mail_from="sender@example.invalid",
            mail_owner="reader@example.invalid",
        ),
    )

    def fail(*_args):
        raise MailError("provider down")

    monkeypatch.setattr(operations, "send", fail)
    text = operations.failure_text("weekly", WHEN, "Pacific/Auckland", ("NZX:MADEUP",))
    with caplog.at_level("WARNING"):
        operations.deliver_operations_notice(
            ScheduleConfig(schedules=[], recipients=["reader@example.invalid"]),
            text,
            recovered=False,
        )
    request.assert_called_once()
    pushed = request.call_args.args[0].data.decode()
    if detail == "full":
        assert pushed == text
    else:
        assert pushed == "A portfolio job failed. Check your email for details."
        assert "NZX:MADEUP" not in pushed
    assert "could not be emailed" in caplog.text
    assert "NZX:MADEUP" in text
    assert not re.search(r"\d", digits_outside_dates(text))
    assert "provider down" not in caplog.text


def test_labels_that_look_like_links_or_figures_are_dropped():
    text = operations.failure_text(
        "prices",
        WHEN,
        "Pacific/Auckland",
        ("NZX:MADEUP (AIR.NZ)", "https://monitor.example/secret", "100.00", "USD/NZD"),
    )
    assert "NZX:MADEUP" in text
    assert "USD/NZD" in text
    assert "AIR.NZ" not in text
    assert "secret" not in text
    assert "100" not in digits_outside_dates(text)


def test_numeric_identifiers_are_readable_without_digits():
    text = operations.failure_text("prices-2", WHEN, "Pacific/Auckland", ("NZX:MADE2",))
    assert "prices-two" in text
    assert "NZX:MADEtwo" in text
    assert not re.search(r"\d", digits_outside_dates(text))


def test_email_uses_report_recipients_and_contains_only_the_operations_notice(monkeypatch):
    from unittest.mock import Mock

    settings = MailSettings(
        _env_file=None, mail_from="sender@example.invalid", mail_owner="fallback@example.invalid"
    )
    monkeypatch.setattr(operations, "mail_settings", lambda: settings)
    send = Mock()
    monkeypatch.setattr(operations, "send", send)
    push = Mock()
    monkeypatch.setattr(operations, "push", push)
    text = operations.recovery_text("prices", WHEN, "Pacific/Auckland")
    operations.deliver_operations_notice(
        ScheduleConfig(schedules=[], recipients=["reader@example.invalid"]), text, recovered=True
    )
    delivered_settings, email, recipients = send.call_args.args
    assert delivered_settings is settings
    assert recipients == ["reader@example.invalid"]
    assert email["Subject"] == "Portfolio job recovered"
    assert email.get_body(preferencelist=("plain",)).get_content().strip() == text
    assert "prices job recovered" in email.get_body(preferencelist=("html",)).get_content()
    push.assert_called_once_with(text)


def test_notice_error_does_not_change_command_result(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SCHEDULER_STATE", str(tmp_path / "scheduler.json"))

    def fail(*args, **kwargs):
        raise RuntimeError("private provider failure 123")

    monkeypatch.setattr(operations, "deliver_operations_notice", fail)
    operations.note_outcome(ScheduleConfig(schedules=[]), "akahu", failed=True, now=WHEN)
    assert "Operations notice could not be delivered." in caplog.text
    assert "private provider" not in caplog.text
