"""Load, preview and deliver year-end summaries without fetching or mutating history."""

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.orm import Session

from portfolio_digest.config import ConfigError
from portfolio_digest.returns import ReturnError
from portfolio_digest.returns_loader import load_inputs
from portfolio_digest.year_end import Dividend, YearEnd, calculate_year_end, tax_year
from portfolio_digest.year_end_report import render_csv, render_html


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMTP_", env_file=".env", extra="ignore")
    host: str = ""
    port: int = 587
    username: str = ""
    password: SecretStr | None = None
    sender: str = ""
    recipient: str = ""


def load_year_end(session: Session, name: str, year: int) -> YearEnd:
    start, end = tax_year(year)
    data = load_inputs(session, name, f"{start}:{end}")
    if data.base_currency != "NZD":
        raise ReturnError("NZ tax-year summaries require an NZD portfolio base currency.")
    labels = {i.id: f"{i.exchange}:{i.code}" for i in data.instruments}
    income = tuple(
        Dividend(
            labels[p.instrument_id],
            p.paid_date,
            p.currency,
            p.gross_amount,
            p.imputation_credits,
            p.withholding_tax,
            p.net_amount,
            p.fx_rate,
        )
        for p in data.income
    )
    return calculate_year_end(data.holdings, income, data.rates, year)


def latest_ended_year(now: datetime | None = None) -> int:
    local = (now or datetime.now(ZoneInfo("Pacific/Auckland"))).astimezone(
        ZoneInfo("Pacific/Auckland")
    )
    return local.year if local.month >= 4 else local.year - 1


def build_message(report: YearEnd, name: str, settings: MailSettings) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Portfolio Digest: {name}, year ended 31 March {report.year}"
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message.set_content(
        f"Your NZ tax-year summary for {report.year} is attached as CSV. "
        "View the HTML email for the summary and notes. Information, not tax advice."
    )
    message.add_alternative(render_html(report, name), subtype="html")
    message.add_attachment(
        render_csv(report).encode("utf-8"),
        maintype="text",
        subtype="csv",
        filename=f"year-end-{report.year}.csv",
    )
    return message


def deliver(report: YearEnd, name: str, *, dry_run: bool, out: Path | None = None) -> None:
    if dry_run:
        if out is None:
            raise ReturnError("--dry-run requires --out DIR.")
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Fixed names cannot escape the output directory through an untrusted portfolio name.
        for suffix, content in (("html", render_html(report, name)), ("csv", render_csv(report))):
            path = out / f"year-end-{report.year}.{suffix}"
            with path.open("w", encoding="utf-8", newline="") as stream:
                path.chmod(0o600)
                stream.write(content)
        return
    try:
        settings = MailSettings()
    except ValidationError:
        raise ConfigError(
            "Invalid SMTP settings; check SMTP_PORT and other SMTP settings."
        ) from None
    if not settings.host or not settings.sender or not settings.recipient:
        raise ConfigError("Set SMTP_HOST, SMTP_SENDER and SMTP_RECIPIENT to send year-end email.")
    try:
        message = build_message(report, name, settings)
        with smtplib.SMTP(settings.host, settings.port, timeout=30) as client:
            client.starttls(context=ssl.create_default_context())
            if settings.username:
                if not settings.password:
                    raise ConfigError("Set SMTP_PASSWORD when SMTP_USERNAME is set.")
                client.login(settings.username, settings.password.get_secret_value())
            client.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        raise ConfigError(
            "Year-end email could not be sent; check SMTP settings and server."
        ) from None
