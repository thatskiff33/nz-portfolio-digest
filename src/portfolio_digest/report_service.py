"""Report orchestration, shared by the CLI and scheduler."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import MailSettings, mail_settings, message, send
from portfolio_digest.report_returns import PeriodEngine, ReportError
from portfolio_digest.reports import build_report, render
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
    schedule = config.named_report(schedule_name)
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone)).date()
    start, end = schedule.window(today)
    report = build_report(PeriodEngine(session), start, end)
    html, text = render(report)
    if dry_run:
        if out is None or out.suffix.lower() not in {".html", ".htm"}:
            raise ConfigError("Dry-run requires --out with an .html or .htm filename.")
        try:
            out.write_text(html, encoding="utf-8")
            out.with_suffix(".txt").write_text(text, encoding="utf-8")
        except OSError:
            raise ReportError(
                "Cannot write report preview files. Check the output directory."
            ) from None
        logger.info("Report preview written. No email sent.")
        return
    if out is not None:
        raise ConfigError("--out is only used with --dry-run.")
    settings = settings or mail_settings()
    recipients = config.recipients or ([settings.mail_owner] if settings.mail_owner else [])
    msg = message(settings.mail_from, recipients, f"Portfolio digest | {end:%d %b %Y}", html, text)
    send(settings, msg, recipients)
    logger.info("Portfolio report sent successfully.")
    ping_kuma(config)
