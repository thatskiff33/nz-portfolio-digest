"""Database boundary for the pure returns engine. No writes or remote price fetching."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.config import get_settings
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.calendars import FX
from portfolio_digest.returns import (
    Holding,
    Observation,
    Payment,
    Report,
    Result,
    ReturnError,
    SplitEvent,
    Transaction,
    _lookup,
    calculate_returns,
    period_range,
    subtotal,
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
    conversion_flags: frozenset[str]


def load_returns(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    gross_up: bool = False,
    base_currency: str | None = None,
) -> Report:
    data = load_inputs(session, name, period, today, base_currency=base_currency)
    report = calculate_returns(
        data.holdings, data.start, data.end, data.rates, data.base_currency, gross_up=gross_up
    )
    if data.conversion_flags:
        rows = tuple(
            replace(r, flags=tuple(sorted(set(r.flags) | data.conversion_flags)))
            for r in report.holdings
        )
        return _combined_report(rows, data.start, data.end, data.base_currency)
    return report


def load_inputs(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    base_currency: str | None = None,
) -> PortfolioInputs:
    today = today or datetime.now(ZoneInfo(get_settings().timezone)).date()
    # Validate syntax before querying; 'all' is resolved from stored activity below.
    start, end = period_range(period, today)
    portfolio = session.scalar(
        select(Portfolio).where(func.lower(Portfolio.name) == portfolio_name(name).lower())
    )
    if portfolio is None:
        raise ReturnError("Portfolio not found.")
    if period == "all":
        start, end = period_range(period, today, first_activity_date(session, portfolio.id, end))
    return load_portfolio_inputs(session, portfolio, start, end, base_currency=base_currency)


def first_activity_date(session: Session, portfolio_id: int, end: date) -> date | None:
    """Earliest reportable stored activity, including income without trades."""
    first_trade = session.scalar(
        select(func.min(Trade.trade_date)).where(
            Trade.portfolio_id == portfolio_id, Trade.trade_date <= end
        )
    )
    first_income = session.scalar(
        select(func.min(Income.paid_date)).where(
            Income.portfolio_id == portfolio_id,
            Income.paid_date <= end,
            Income.kind.in_(("dividend", "distribution")),
        )
    )
    return min((day for day in (first_trade, first_income) if day is not None), default=None)


def load_portfolio_inputs(
    session: Session,
    portfolio: Portfolio,
    start: date,
    end: date,
    *,
    base_currency: str | None = None,
) -> PortfolioInputs:
    """Load stored inputs once for reports and sampled dashboard valuations."""
    target_currency = base_currency or portfolio.base_currency
    trades = session.scalars(
        select(Trade)
        .where(Trade.portfolio_id == portfolio.id, Trade.trade_date <= end)
        .order_by(Trade.trade_date, Trade.id)
    ).all()
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
            FxRate.base_currency == target_currency,
            FxRate.rate_date <= end,
        )
    ).all()
    rates: dict[str, tuple[Observation, ...]] = {}
    for currency in {r.currency for r in fx_rows}:
        rates[currency] = tuple(
            Observation(r.rate_date, r.rate, r.is_stale) for r in fx_rows if r.currency == currency
        )
    conversion_flags: set[str] = set()

    def recorded_fx(value, day, currency):
        if target_currency == portfolio.base_currency:
            return value
        if day <= start or value is None:
            return None
        if currency == target_currency:
            # Native target-currency amounts need no conversion. Broker FX and
            # market cross-rates need not be exact reciprocals.
            return None
        # Broker FX is relative to the portfolio base. Translate that base using
        # the same dated lookup and stale-data rules as every other engine input.
        with localcontext() as context:
            context.prec = max(context.prec, 80)
            return value * _lookup(
                rates.get(portfolio.base_currency, ()),
                day,
                f"{portfolio.base_currency}/{target_currency} FX",
                conversion_flags,
                FX,
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
                        recorded_fx(t.fx_rate, t.trade_date, t.currency),
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
                        recorded_fx(p.fx_rate, p.paid_date, p.currency),
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
                instrument.name,
            )
        )
    return PortfolioInputs(
        target_currency,
        tuple(holdings),
        rates,
        tuple(income),
        tuple(instruments),
        start,
        end,
        frozenset(conversion_flags),
    )


def _combined_report(rows: tuple[Result, ...], start: date, end: date, currency: str) -> Report:
    markets = tuple(
        subtotal(m, tuple(r for r in rows if r.exchange == m))
        for m in sorted({r.exchange for r in rows})
    )
    return Report(start, end, currency, rows, markets, subtotal("Portfolio", rows))


def load_combined_returns(session: Session, start: date, end: date) -> Report:
    """All portfolios in NZD, aggregated by listing using shared engine subtotals."""
    reports = [
        load_returns(session, p.name, f"{start}:{end}", base_currency="NZD")
        for p in session.scalars(select(Portfolio).order_by(Portfolio.id))
    ]
    rows = [r for report in reports for r in report.holdings]
    combined = []
    for label in sorted({r.label for r in rows}):
        group = tuple(r for r in rows if r.label == label)
        first = group[0]
        combined.append(
            replace(
                subtotal(label, group),
                exchange=first.exchange,
                price=next((r.price for r in group if r.price is not None), None),
                currency=first.currency,
                name=first.name,
            )
        )
    return _combined_report(tuple(combined), start, end, "NZD")
