"""Exact market valuations, isolated for integration with the upcoming returns engine.

No cash balance or investment performance is inferred from incomplete trade history.
Missing quotes/FX make totals unavailable rather than silently understating a portfolio.
"""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.models import FxRate, Instrument, Portfolio, Price, Trade

ZERO = Decimal(0)


@dataclass
class Holding:
    instrument: Instrument
    quantity: Decimal
    price: Decimal | None
    quote_date: date | None
    value: Decimal | None
    stale: bool


@dataclass
class Snapshot:
    day: date
    holdings: list[Holding]

    @property
    def value(self) -> Decimal | None:
        if any(row.value is None for row in self.holdings):
            return None
        return sum((row.value for row in self.holdings), ZERO)


class Valuations:
    """Load once, value many days without issuing a query per day or holding."""

    def __init__(self, session: Session, portfolio: Portfolio, end: date):
        self.portfolio = portfolio
        self.trades = list(
            session.scalars(
                select(Trade)
                .where(Trade.portfolio_id == portfolio.id, Trade.trade_date <= end)
                .order_by(Trade.trade_date, Trade.id)
            )
        )
        ids = {t.instrument_id for t in self.trades}
        self.instruments = {
            i.id: i for i in session.scalars(select(Instrument).where(Instrument.id.in_(ids)))
        }
        self.prices = defaultdict(list)
        for p in session.scalars(
            select(Price)
            .where(Price.instrument_id.in_(ids), Price.price_date <= end)
            .order_by(Price.price_date)
        ):
            self.prices[p.instrument_id].append(p)
        self.fx = defaultdict(list)
        for rate in session.scalars(
            select(FxRate)
            .where(FxRate.base_currency == portfolio.base_currency, FxRate.rate_date <= end)
            .order_by(FxRate.rate_date)
        ):
            self.fx[rate.currency].append(rate)
        self.price_days = {key: [p.price_date for p in rows] for key, rows in self.prices.items()}
        self.fx_days = {key: [r.rate_date for r in rows] for key, rows in self.fx.items()}

    def at(self, day: date) -> Snapshot:
        units = defaultdict(lambda: ZERO)
        for t in self.trades:
            if t.trade_date > day:
                break
            units[t.instrument_id] += -t.quantity if t.trade_type == "sell" else t.quantity
        holdings = []
        for ident, quantity in units.items():
            if not quantity:
                continue
            instrument = self.instruments[ident]
            idx = bisect_right(self.price_days.get(ident, []), day) - 1
            quote = self.prices[ident][idx] if idx >= 0 else None
            fx = Decimal(1) if instrument.currency == self.portfolio.base_currency else None
            fx_stale = False
            if fx is None:
                idx = bisect_right(self.fx_days.get(instrument.currency, []), day) - 1
                rate = self.fx[instrument.currency][idx] if idx >= 0 else None
                if rate:
                    fx = rate.rate
                    fx_stale = rate.is_stale or rate.rate_date < day
            value = quantity * quote.close * fx if quote is not None and fx is not None else None
            holdings.append(
                Holding(
                    instrument,
                    quantity,
                    quote.close if quote else None,
                    quote.price_date if quote else None,
                    value,
                    quote is None
                    or quote.is_stale
                    or quote.price_date < day
                    or fx_stale
                    or fx is None,
                )
            )
        return Snapshot(day, holdings)

    def series(self, start: date, end: date) -> list[tuple[date, Decimal | None]]:
        # At most 180 evenly spaced samples, always including both endpoints.
        days = (end - start).days
        offsets = (
            sorted({days * n // min(days, 179) for n in range(min(days, 179) + 1)}) if days else [0]
        )
        return [
            (start + timedelta(days=n), self.at(start + timedelta(days=n)).value) for n in offsets
        ]


def period_dates(period: str, today: date, first: date, start: str = "", end: str = ""):
    if period == "custom":
        begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    else:
        finish = today
        options = {
            "week": today - timedelta(days=7),
            "month": today - timedelta(days=30),
            "ytd": date(today.year, 1, 1),
            "all": min(first, today),
        }
        if period not in options:
            raise ValueError("Choose a valid period.")
        begin = options[period]
    if begin > finish or finish > today:
        raise ValueError("Choose dates in order, ending no later than today.")
    return begin, finish
