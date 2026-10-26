"""Data-quality checks for stored prices and exchange rates."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.models import FxRate, Price, Split
from portfolio_digest.prices import calendars
from portfolio_digest.prices.fetch import CurrencyNeed, Holding, currencies_needed

JUMP_LIMIT = Decimal("0.50")


@dataclass(frozen=True)
class QualityFinding:
    """One actionable problem found in stored market data."""

    kind: str
    label: str
    day: date
    detail: str

    def describe(self) -> str:
        return f"{self.kind}\t{self.label}\t{self.day.isoformat()}\t{self.detail}"


def check_data(
    session: Session, holdings: Iterable[Holding], base_currency: str, now: datetime
) -> list[QualityFinding]:
    """Check expected price and FX ranges, returning findings in a stable order."""
    holdings = list(holdings)
    findings: list[QualityFinding] = []
    for holding in holdings:
        findings.extend(_check_instrument(session, holding, now))
    for need in currencies_needed(holdings, base_currency, now):
        findings.extend(_check_currency(session, need))
    return sorted(findings, key=lambda item: (item.day, item.label, item.kind))


def _check_instrument(session: Session, holding: Holding, now: datetime) -> list[QualityFinding]:
    end = holding.last_day_needed(now)
    if holding.first_trade > end:
        return []
    rows = session.execute(
        select(Price.price_date, Price.close, Price.is_stale)
        .where(
            Price.instrument_id == holding.instrument_id,
            Price.price_date.between(holding.first_trade, end),
        )
        .order_by(Price.price_date)
    ).all()
    by_day = {day: close for day, close, _stale in rows}
    findings = [
        QualityFinding("missing-price", holding.label, day, "no price stored")
        for day in holding.calendar.trading_days(holding.first_trade, end)
        if day not in by_day
    ]
    findings.extend(
        QualityFinding("zero-price", holding.label, day, "closing price is zero")
        for day, close, _stale in rows
        if close == 0
    )
    findings.extend(
        QualityFinding("stale-price", holding.label, day, "last known price was carried forward")
        for day, _close, stale in rows
        if stale
    )

    split_days = set(
        session.scalars(
            select(Split.split_date).where(
                Split.instrument_id == holding.instrument_id,
                Split.split_date.between(holding.first_trade, end),
            )
        )
    )
    fresh = [(day, close) for day, close, stale in rows if not stale]
    for (previous_day, previous_close), (day, close) in pairwise(fresh):
        if (
            previous_close > 0
            and close > 0
            and not any(previous_day < split_day <= day for split_day in split_days)
        ):
            change = abs(close / previous_close - 1)
            if change > JUMP_LIMIT:
                percent = (change * 100).quantize(Decimal("0.1"))
                findings.append(
                    QualityFinding(
                        "suspicious-jump",
                        holding.label,
                        day,
                        f"close changed {percent}% since {previous_day.isoformat()} "
                        "without a split",
                    )
                )
    return findings


def _check_currency(session: Session, need: CurrencyNeed) -> list[QualityFinding]:
    rows = session.execute(
        select(FxRate.rate_date, FxRate.is_stale).where(
            FxRate.currency == need.currency,
            FxRate.base_currency == need.base_currency,
            FxRate.rate_date.between(need.first_day, need.last_day),
        )
    ).all()
    stored = {day for day, _stale in rows}
    findings = [
        QualityFinding("missing-fx", need.label, day, "no exchange rate stored")
        for day in calendars.FX.trading_days(need.first_day, need.last_day)
        if day not in stored
    ]
    findings.extend(
        QualityFinding("stale-fx", need.label, day, "last known rate was carried forward")
        for day, stale in rows
        if stale
    )
    return findings
