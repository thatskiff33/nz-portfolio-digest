"""Report orchestration, shared by the CLI and scheduler."""

import logging
import os
from datetime import datetime
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.config import ConfigError, runtime_settings
from portfolio_digest.delivery import portfolio_delivery
from portfolio_digest.mailers import MailSettings, mail_settings, message, send
from portfolio_digest.models import Portfolio
from portfolio_digest.pings import PingError, ping
from portfolio_digest.reports import ReportError, build_report, render
from portfolio_digest.scheduling import DeliveryCheckpoint, ScheduleConfig, SchedulerStateError

logger = logging.getLogger(__name__)


def ping_monitor(url: SecretStr | None) -> None:
    """Ping an Uptime Kuma push URL. The URL is never logged."""
    if url is None or not url.get_secret_value():
        return
    try:
        ping(url.get_secret_value())
    except PingError:
        logger.warning("The monitoring push failed.")


def ping_kuma(config: ScheduleConfig) -> None:
    ping_monitor(config.kuma_push_url)


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
    checkpoint: DeliveryCheckpoint | None = None,
) -> None:
    _validate_preview(dry_run, out)
    schedule = config.named_report(schedule_name)
    today = runtime_settings().today(now)
    if schedule.period == "tax_year":
        from portfolio_digest.year_end_service import latest_ended_year, run_year_end

        year = latest_ended_year(now)
        portfolios = session.execute(
            select(Portfolio.id, Portfolio.name)
            .where(Portfolio.base_currency == "NZD")
            .order_by(Portfolio.id)
        ).all()
        failed = False
        for portfolio_id, name in portfolios:
            if not dry_run and checkpoint and portfolio_id in checkpoint.delivered:
                continue
            preview = (
                out.with_name(f"{out.stem}-portfolio-{portfolio_id}{out.suffix}") if out else None
            )
            try:
                with portfolio_delivery(portfolio_id):
                    run_year_end(
                        session,
                        config,
                        name,
                        year,
                        dry_run=dry_run,
                        out=preview,
                        settings=settings,
                    )
            except SchedulerStateError:
                raise
            except Exception:
                session.rollback()
                failed = True
                logger.warning("Portfolio year-end delivery failed.")
                continue
            if not dry_run and checkpoint:
                checkpoint.complete(portfolio_id)
        if failed:
            raise ReportError(
                "Some portfolio year-end reports failed. Check report data and delivery settings."
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


def _write_private(path: Path, content: str) -> None:
    if os.name != "posix":
        path.write_text(content, encoding="utf-8")
        return
    # Tighten permissions before truncating an existing preview.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(content)
    finally:
        os.close(descriptor)


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
            _write_private(out, html)
            _write_private(out.with_suffix(".txt"), text)
            if csv is not None:
                _write_private(out.with_suffix(".csv"), csv)
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
