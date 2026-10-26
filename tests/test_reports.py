"""Offline return attribution, report rendering, previews and delivery orchestration."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from pydantic import SecretStr

from portfolio_digest import cli, report_service
from portfolio_digest.mailers import MailError, MailSettings
from portfolio_digest.reports import Report, ValuePoint, render
from portfolio_digest.returns import Components, Result, subtotal
from portfolio_digest.returns import Report as ReturnsReport
from portfolio_digest.scheduling import ScheduleConfig

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    def __init__(self, **rows):
        self.rows = rows

    def scalars(self, statement):
        return self.rows.get(statement.column_descriptions[0]["name"], [])


def example_report():
    rows = (
        Result(
            "ASX:DEMO",
            D("191"),
            D("187"),
            D(0),
            D(0),
            Components(D("-5"), D("2"), D("1")),
            ("Stale example",),
            "ASX",
            gross_dividends=D(3),
            withholding_tax=D(1),
            imputation_gross_up=D(".5"),
            units=D(20),
            price=D("8.5"),
            currency="AUD",
            name="Demo & Co",
        ),
        Result(
            "NZX:ALP",
            D("1200"),
            D("1234"),
            D(0),
            D(0),
            Components(D(34), D(7), D(0)),
            exchange="NZX",
            gross_dividends=D(10),
            withholding_tax=D(3),
            imputation_gross_up=D(2),
            units=D(100),
            price=D("12.34"),
            currency="NZD",
            name="Alpine Example",
        ),
    )
    returns = ReturnsReport(
        date(2026, 10, 4),
        date(2026, 10, 11),
        "NZD",
        rows,
        tuple(subtotal(r.exchange, (r,)) for r in rows),
        subtotal("Portfolio", rows),
    )
    return Report(
        date(2026, 10, 5),
        date(2026, 10, 11),
        returns,
        (ValuePoint(date(2026, 10, 4), D("1391")), ValuePoint(date(2026, 10, 11), D("1421"))),
    )


def test_html_and_text_match_reviewable_synthetic_snapshots():
    html, text = render(example_report())
    assert html == (FIXTURES / "report.html").read_text()
    assert text == (FIXTURES / "report.txt").read_text()
    assert "NZ$ 1,421.00" in html
    assert html.count("Subtotal") == 4
    assert "(stale)" in html
    assert "Currency gain" in text


def test_html_escapes_untrusted_instrument_labels():
    report = example_report()
    bad = replace(report.returns.holdings[0], label="<script>:<img src=x>", exchange="<script>")
    returns = replace(report.returns, holdings=(bad,), markets=(subtotal("<script>", (bad,)),))
    html, _ = render(Report(report.start, report.end, returns))
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


def test_email_rounds_half_cents_like_cli():
    from portfolio_digest.reports import money
    from portfolio_digest.returns_table import format_number

    assert money(D("1.005")) == format_number(D("1.005")) == "1.01"
    assert money(D("-1.005")) == format_number(D("-1.005")) == "-1.01"


def test_shared_change_retains_decimal_precision():
    from decimal import localcontext

    row = replace(
        example_report().returns.total,
        closing=D("10000000000000000000000000000.01"),
        opening=D("10000000000000000000000000000"),
    )
    with localcontext() as context:
        context.prec = 10
        assert row.change == D(".01")
