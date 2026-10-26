"""Database boundary for the pure returns engine. No writes or remote price fetching."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.config import get_settings
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.returns import (
    Holding,
    Observation,
    Payment,
    Report,
    ReturnError,
    SplitEvent,
    Transaction,
    calculate_returns,
    period_range,
)
from portfolio_digest.validation import portfolio_name


@dataclass(frozen=True)
class PortfolioInputs:
    base_currency: str
    holdings: tuple[Holding, ...]
    rates: dict[str, tuple[Observation, ...]]
    income: tuple[Income, ...]
    instruments: tuple[Instrument, ...]
    start: date
    end: date


def load_returns(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    gross_up: bool = False,
) -> Report:
    data = load_inputs(session, name, period, today)
    return calculate_returns(
        data.holdings, data.start, data.end, data.rates, data.base_currency, gross_up=gross_up
    )


def load_inputs(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
) -> PortfolioInputs:
    today = today or datetime.now(ZoneInfo(get_settings().timezone)).date()
    # Validate syntax before querying; 'all' is resolved after loading trades.
    start, end = period_range(period, today)
    portfolio = session.scalar(
        select(Portfolio).where(func.lower(Portfolio.name) == portfolio_name(name).lower())
    )
    if portfolio is None:
        raise ReturnError("Portfolio not found.")
    trades = session.scalars(
        select(Trade)
        .where(Trade.portfolio_id == portfolio.id, Trade.trade_date <= end)
        .order_by(Trade.trade_date, Trade.id)
    ).all()
    if period == "all":
        first_income = session.scalar(
            select(func.min(Income.paid_date)).where(
                Income.portfolio_id == portfolio.id,
                Income.paid_date <= end,
                Income.kind.in_(("dividend", "distribution")),
            )
        )
        dates = [t.trade_date for t in trades]
        if first_income is not None:
            dates.append(first_income)
        start, end = period_range(period, today, min(dates, default=None))
    income = session.scalars(
        select(Income).where(
            Income.portfolio_id == portfolio.id,
            Income.paid_date > start,
            Income.paid_date <= end,
            Income.kind.in_(("dividend", "distribution")),
        )
    ).all()
    ids = {t.instrument_id for t in trades} | {p.instrument_id for p in income}
    instruments = session.scalars(select(Instrument).where(Instrument.id.in_(ids))).all()
    prices = session.scalars(
        select(Price).where(
            Price.instrument_id.in_(ids),
            Price.price_date <= end,
        )
    ).all()
    split_rows = session.scalars(
        select(Split).where(Split.instrument_id.in_(ids), Split.split_date <= end)
    ).all()
    fx_rows = session.scalars(
        select(FxRate).where(
            FxRate.base_currency == portfolio.base_currency,
            FxRate.rate_date <= end,
        )
    ).all()
    rates: dict[str, tuple[Observation, ...]] = {}
    for currency in {r.currency for r in fx_rows}:
        rates[currency] = tuple(
            Observation(r.rate_date, r.rate, r.is_stale) for r in fx_rows if r.currency == currency
        )
    holdings = []
    for instrument in instruments:
        instrument_trades = [t for t in trades if t.instrument_id == instrument.id]
        if any(t.currency != instrument.currency for t in instrument_trades):
            raise ReturnError(
                "Trade currency differs from instrument currency; normalise it first."
            )
        holdings.append(
            Holding(
                instrument.exchange,
                instrument.code,
                instrument.currency,
                tuple(
                    Transaction(
                        t.trade_date,
                        t.trade_type,
                        t.quantity,
                        t.consideration / t.quantity if t.consideration is not None else t.price,
                        t.fees,
                        t.fx_rate,
                    )
                    for t in instrument_trades
                ),
                tuple(
                    Observation(p.price_date, p.close, p.is_stale)
                    for p in prices
                    if p.instrument_id == instrument.id
                ),
                tuple(
                    Payment(
                        p.paid_date,
                        p.net_amount,
                        p.currency,
                        p.fx_rate,
                        p.gross_amount,
                        p.withholding_tax,
                        p.imputation_credits,
                    )
                    for p in income
                    if p.instrument_id == instrument.id
                ),
                tuple(
                    SplitEvent(
                        s.split_date, s.shares_before, s.shares_after, s.source == "registry"
                    )
                    for s in split_rows
                    if s.instrument_id == instrument.id
                ),
            )
        )
    return PortfolioInputs(
        portfolio.base_currency,
        tuple(holdings),
        rates,
        tuple(income),
        tuple(instruments),
        start,
        end,
    )
