"""Database boundary for the pure returns engine. No writes or remote price fetching."""

from dataclasses import replace
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


def load_returns(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    gross_up: bool = False,
    base_currency: str | None = None,
) -> Report:
    today = today or datetime.now(ZoneInfo(get_settings().timezone)).date()
    # Validate syntax before querying; 'all' is resolved after loading trades.
    start, end = period_range(period, today)
    portfolio = session.scalar(
        select(Portfolio).where(func.lower(Portfolio.name) == portfolio_name(name).lower())
    )
    if portfolio is None:
        raise ReturnError("Portfolio not found.")
    target_currency = base_currency or portfolio.base_currency
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

    def recorded_fx(value, day):
        if day <= start:
            return None
        if value is None or target_currency == portfolio.base_currency:
            return value
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
                        recorded_fx(t.fx_rate, t.trade_date),
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
                        recorded_fx(p.fx_rate, p.paid_date),
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
    report = calculate_returns(
        tuple(holdings), start, end, rates, target_currency, gross_up=gross_up
    )
    if conversion_flags:
        rows = tuple(
            replace(r, flags=tuple(sorted(set(r.flags) | conversion_flags)))
            for r in report.holdings
        )
        return _combined_report(rows, start, end, target_currency)
    return report


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
