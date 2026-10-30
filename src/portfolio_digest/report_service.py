"""Report orchestration, shared by the CLI and scheduler."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import MailSettings, mail_settings, message, send
from portfolio_digest.models import Portfolio
from portfolio_digest.reports import ReportError, build_report, render
from portfolio_digest.scheduling import ScheduleConfig

logger = logging.getLogger(__name__)


def ping_kuma(config: ScheduleConfig) -> None:
    if not config.kuma_push_url:
        return
    try:
        url = config.kuma_push_url.get_secret_value()
        if urlsplit(url).scheme not in {"https", "http"}:
            raise ValueError
        # urllib does not emit HTTP request URLs (the push URL contains a secret).
        with urlopen(url, timeout=10) as response:  # noqa: S310
            if response.status != 200:
                raise ValueError
    except Exception:
        logger.warning("Report sent, but the monitoring push failed.")


def _validate_preview(dry_run: bool, out: Path | None) -> None:
    if dry_run and (out is None or out.suffix.lower() not in {".html", ".htm"}):
        raise ConfigError("Dry-run requires --out with an .html or .htm filename.")
    if not dry_run and out is not None:
        raise ConfigError("--out is only used with --dry-run.")


def run_report(
    session: Session,
    config: ScheduleConfig,
    schedule_name: str,
    *,
    dry_run: bool = False,
    out: Path | None = None,
    now: datetime | None = None,
    settings: MailSettings | None = None,
) -> None:
    _validate_preview(dry_run, out)
    schedule = config.named_report(schedule_name)
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone)).date()
    if schedule.period == "tax_year":
        from portfolio_digest.year_end_service import latest_ended_year, run_year_end

        year = latest_ended_year(now)
        portfolios = (
            select(Portfolio).where(Portfolio.base_currency == "NZD").order_by(Portfolio.id)
        )
        for portfolio in session.scalars(portfolios):
            preview = (
                out.with_name(f"{out.stem}-portfolio-{portfolio.id}{out.suffix}") if out else None
            )
            run_year_end(
                session,
                config,
                portfolio.name,
                year,
                dry_run=dry_run,
                out=preview,
                settings=settings,
            )
        if not dry_run:
            ping_kuma(config)
        return
    start, end = schedule.window(today)
    report = build_report(session, start, end)
    html, text = render(report)
    deliver_report(
        config,
        f"Portfolio digest | {end:%d %b %Y}",
        html,
        text,
        dry_run=dry_run,
        out=out,
        settings=settings,
    )
    if not dry_run:
        ping_kuma(config)


def deliver_report(
    config: ScheduleConfig,
    subject: str,
    html: str,
    text: str,
    *,
    dry_run: bool = False,
    out: Path | None = None,
    settings: MailSettings | None = None,
    csv: str | None = None,
    csv_name: str = "report.csv",
) -> None:
    """Shared preview and mailer boundary for every report."""
    _validate_preview(dry_run, out)
    if dry_run:
        try:
            out.write_text(html, encoding="utf-8")
            out.with_suffix(".txt").write_text(text, encoding="utf-8")
            if csv is not None:
                out.with_suffix(".csv").write_text(csv, encoding="utf-8")
        except OSError:
            raise ReportError(
                "Cannot write report preview files. Check the output directory."
            ) from None
        logger.info("Report preview written. No email sent.")
        return
    settings = settings or mail_settings()
    recipients = config.recipients or ([settings.mail_owner] if settings.mail_owner else [])
    msg = message(settings.mail_from, recipients, subject, html, text)
    if csv is not None:
        msg.add_attachment(csv.encode("utf-8"), maintype="text", subtype="csv", filename=csv_name)
    send(settings, msg, recipients)
    logger.info("Portfolio report sent successfully.")
