"""Presentation metadata around the shared returns engine, with no valuation maths."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.models import Instrument, Portfolio
from portfolio_digest.returns import (
    Report,
    Result,
    ReturnError,
    calculate_returns,
    holding_units_at,
    period_range,
)
from portfolio_digest.returns_loader import load_portfolio_inputs


@dataclass
class Holding:
    instrument: Instrument
    quantity: Decimal
    price: Decimal | None
    quote_date: date | None
    result: Result | None

    @property
    def value(self) -> Decimal | None:
        return self.result.closing if self.result else None


class Dashboard:
    def __init__(self, session: Session, portfolio: Portfolio, start: date, end: date):
        self.inputs, self.rates = (), {}
        self.currency = portfolio.base_currency
        self.error = None
        try:
            data = load_portfolio_inputs(session, portfolio, start, end)
            self.inputs, self.rates = data.holdings, data.rates
            self.report = self.calculate(start, end)
        except ReturnError as exc:
            self.report = None
            self.error = str(exc)
        results = {r.label: r for r in self.report.holdings} if self.report else {}
        keys = {(h.exchange, h.code) for h in self.inputs}
        instruments = {
            (i.exchange, i.code): i
            for i in session.scalars(select(Instrument))
            if (i.exchange, i.code) in keys
        }
        self.holdings = []
        for holding in self.inputs:
            result = results.get(f"{holding.exchange}:{holding.code}")
            if self.report and result is None:
                continue
            quote = max(holding.prices, key=lambda p: p.day, default=None)
            self.holdings.append(
                Holding(
                    instruments[holding.exchange, holding.code],
                    holding_units_at(holding, end),
                    quote.value if quote else None,
                    quote.day if quote else None,
                    result,
                )
            )

    def calculate(self, start: date, end: date) -> Report:
        if self.error and not self.inputs:
            raise ReturnError(self.error)
        return calculate_returns(self.inputs, start, end, self.rates, self.currency)

    def series(self, start: date, end: date) -> list[tuple[date, Decimal | None]]:
        # At most 180 samples. Each point is the engine's closing value for that day.
        days = (end - start).days
        offsets = (
            sorted({days * n // min(days, 179) for n in range(min(days, 179) + 1)}) if days else [0]
        )
        points = []
        for offset in offsets:
            day = start + timedelta(days=offset)
            try:
                value = self.calculate(day, day).total.closing
            except ReturnError:
                value = None
            points.append((day, value))
        return points


def period_dates(period: str, today: date, first: date, start: str = "", end: str = ""):
    if period not in {"week", "month", "ytd", "all", "custom"}:
        raise ValueError("Choose a valid period.")
    begin, finish = period_range(f"{start}:{end}" if period == "custom" else period, today, first)
    if finish > today:
        raise ValueError("Choose dates ending no later than today.")
    return begin, finish
