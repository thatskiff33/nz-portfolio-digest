"""Database boundary for the pure returns engine. No writes or remote price fetching."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.config import get_settings
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.parcels import _require_aware, financial_math, lookup
from portfolio_digest.prices.calendars import FX
from portfolio_digest.returns import (
    Holding,
    Observation,
    ObservationSeries,
    Payment,
    Report,
    Result,
    ReturnError,
    SplitEvent,
    Transaction,
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
    as_of: datetime | None = None


def load_returns(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    gross_up: bool = False,
    base_currency: str | None = None,
    as_of: datetime | None = None,
) -> Report:
    data = load_inputs(session, name, period, today, base_currency=base_currency, as_of=as_of)
    report = calculate_returns(
        data.holdings,
        data.start,
        data.end,
        data.rates,
        data.base_currency,
        gross_up=gross_up,
        as_of=data.as_of,
    )
    if data.conversion_flags:
        rows = tuple(
            replace(r, flags=tuple(sorted(set(r.flags) | data.conversion_flags)))
            for r in report.holdings
        )
        return _combined_report(rows, data.start, data.end, data.base_currency)
    return report


@financial_math
def load_inputs(
    session: Session,
    name: str,
    period: str,
    today: date | None = None,
    *,
    base_currency: str | None = None,
    as_of: datetime | None = None,
) -> PortfolioInputs:
    as_of = _require_aware(as_of)
    if today is None:
        # One instant supplies both the live date and the freshness bound.
        zone = ZoneInfo(get_settings().timezone)
        as_of = as_of or datetime.now(zone)
        today = as_of.astimezone(zone).date()
    # Validate syntax before querying; 'all' is resolved from stored activity below.
    start, end = period_range(period, today)
    portfolio = session.scalar(
        select(Portfolio).where(func.lower(Portfolio.name) == portfolio_name(name).lower())
    )
    if portfolio is None:
        raise ReturnError("Portfolio not found.")
    if period == "all":
        start, end = period_range(period, today, first_activity_date(session, portfolio.id, end))
    return load_portfolio_inputs(
        session, portfolio, start, end, base_currency=base_currency, as_of=as_of
    )


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


def _recorded_fx(value, day, currency, origin, target, start, rates, flags, *, as_of=None):
    """Translate broker FX using the report's dates and target currency."""
    if origin == target:
        return value
    if day <= start or value is None or currency == target:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, 80)
        return value * lookup(
            rates.get(origin, ()), day, f"{origin}/{target} FX", flags, FX, as_of=as_of
        )


@financial_math
def load_portfolio_inputs(
    session: Session,
    portfolio: Portfolio,
    start: date,
    end: date,
    *,
    base_currency: str | None = None,
    instrument_id: int | None = None,
    raw_fx: bool = False,
    as_of: datetime | None = None,
) -> PortfolioInputs:
    """Load stored inputs once for reports and sampled dashboard valuations."""
    target_currency = base_currency or portfolio.base_currency
    trades = session.scalars(
        select(Trade)
        .where(
            Trade.portfolio_id == portfolio.id,
            Trade.trade_date <= end,
            True if instrument_id is None else Trade.instrument_id == instrument_id,
        )
        .order_by(Trade.trade_date, Trade.id)
    ).all()
    income = session.scalars(
        select(Income).where(
            Income.portfolio_id == portfolio.id,
            True if instrument_id is None else Income.instrument_id == instrument_id,
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
    prices_by_id = defaultdict(list)
    for price in prices:
        prices_by_id[price.instrument_id].append(
            Observation(price.price_date, price.close, price.is_stale)
        )
    rates_by_currency = defaultdict(list)
    for row in fx_rows:
        rates_by_currency[row.currency].append(Observation(row.rate_date, row.rate, row.is_stale))
    rates = {currency: ObservationSeries(points) for currency, points in rates_by_currency.items()}
    conversion_flags: set[str] = set()

    def recorded_fx(value, day, currency):
        if raw_fx:
            return value
        return _recorded_fx(
            value,
            day,
            currency,
            portfolio.base_currency,
            target_currency,
            start,
            rates,
            conversion_flags,
            as_of=as_of,
        )

    trades_by_id = defaultdict(list)
    income_by_id = defaultdict(list)
    splits_by_id = defaultdict(list)
    for trade in trades:
        trades_by_id[trade.instrument_id].append(trade)
    for payment in income:
        income_by_id[payment.instrument_id].append(payment)
    for split in split_rows:
        splits_by_id[split.instrument_id].append(split)

    holdings = []
    for instrument in instruments:
        instrument_trades = trades_by_id[instrument.id]
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
                ObservationSeries(prices_by_id[instrument.id]),
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
                    for p in income_by_id[instrument.id]
                ),
                tuple(
                    SplitEvent(
                        s.split_date, s.shares_before, s.shares_after, s.source == "registry"
                    )
                    for s in splits_by_id[instrument.id]
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
        as_of,
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
    return combine_reports(reports, start, end)


def combine_reports(reports, start: date, end: date) -> Report:
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


def combined_evaluator(session: Session, end: date):
    """Read history once, preserving each sample's dated broker FX conversion."""
    inputs = [
        (
            p.base_currency,
            load_portfolio_inputs(session, p, date.min, end, base_currency="NZD", raw_fx=True),
        )
        for p in session.scalars(select(Portfolio).order_by(Portfolio.id))
    ]

    def evaluate(first: date, last: date) -> Report:
        reports = []
        for origin, data in inputs:
            flags = set()

            def convert(value, day, currency, origin=origin, data=data, flags=flags):
                return _recorded_fx(value, day, currency, origin, "NZD", first, data.rates, flags)

            holdings = tuple(
                replace(
                    h,
                    trades=tuple(
                        replace(t, fx=convert(t.fx, t.day, h.currency))
                        for t in h.trades
                        if t.day <= last
                    ),
                    income=tuple(
                        replace(p, fx=convert(p.fx, p.day, p.currency))
                        for p in h.income
                        if first < p.day <= last
                    ),
                )
                for h in data.holdings
            )
            report = calculate_returns(holdings, first, last, data.rates, "NZD")
            rows = tuple(
                replace(r, flags=tuple(sorted(set(r.flags) | flags))) for r in report.holdings
            )
            reports.append(_combined_report(rows, first, last, "NZD"))
        return combine_reports(reports, first, last)

    return evaluate
