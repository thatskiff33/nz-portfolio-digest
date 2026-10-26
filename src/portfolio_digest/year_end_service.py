"""Year-end orchestration using the shared returns inputs and report delivery."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from portfolio_digest.mailers import MailSettings
from portfolio_digest.returns import ReturnError
from portfolio_digest.returns_loader import load_inputs
from portfolio_digest.scheduling import ScheduleConfig
from portfolio_digest.year_end import Dividend, YearEnd, calculate_year_end, tax_year
from portfolio_digest.year_end_report import render_csv, render_html, render_text


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


def run_year_end(
    session: Session,
    config: ScheduleConfig,
    name: str,
    year: int,
    *,
    dry_run: bool = False,
    out: Path | None = None,
    settings: MailSettings | None = None,
) -> None:
    # Local import keeps report dispatch and the year-end builder independent.
    from portfolio_digest.report_service import deliver_report

    report = load_year_end(session, name, year)
    deliver_report(
        config,
        f"Portfolio Digest: {name}, year ended 31 March {year}",
        render_html(report, name),
        render_text(report, name),
        dry_run=dry_run,
        out=out,
        settings=settings,
        csv=render_csv(report),
        csv_name=f"year-end-{year}.csv",
    )
