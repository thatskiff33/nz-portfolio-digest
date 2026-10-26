"""Exact Decimal period performance and compatible lifetime ledger helpers.

Each opening or purchased FIFO parcel has quantity q, local entry price p0 and
entry FX f0 (base currency per local unit). On sale or closing valuation p1, f1:
capital = q * (p1 - p0) * f0; currency = q * p1 * (f1 - f0).
The cross-term belongs to currency. Their sum is q * (p1*f1 - p0*f0).
Fees reduce capital. All percentages share opening value plus purchase outlays
as denominator. Sales are withdrawals, not a reduction in capital put at risk.
Non-split adjustments move units in or out at their recorded price without cash.
A parcel keeps its local cost (q * p0), so a split rescales units exactly.
See docs/RETURNS.md for boundaries, income and missing-data conventions.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from functools import wraps
from itertools import groupby
from typing import ParamSpec, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importing import ImportFailure
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.calendars import FX, TradingCalendar, for_exchange

ZERO = Decimal(0)
HUNDRED = Decimal(100)
P = ParamSpec("P")
T = TypeVar("T")


def _financial_math(function: Callable[P, T]) -> Callable[P, T]:
    """Allow 64-digit schema products plus subtraction and aggregation headroom."""

    @wraps(function)
    def exact(*args: P.args, **kwargs: P.kwargs) -> T:
        with localcontext() as context:
            context.prec = max(context.prec, 80)
            return function(*args, **kwargs)

    return exact


class ReturnError(ValueError):
    """Incomplete or inconsistent inputs prevent a trustworthy report."""


@dataclass(frozen=True)
class Observation:
    day: date
    value: Decimal
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal) or not self.value.is_finite() or self.value < ZERO:
            raise ReturnError("Observations must be finite, non-negative Decimals.")


@dataclass(frozen=True)
class Transaction:
    day: date
    kind: str
    quantity: Decimal
    price: Decimal
    fees: Decimal = ZERO
    fx: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (self.quantity, self.price, self.fees):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ReturnError("Transactions require finite Decimals.")
        if self.kind not in {"buy", "sell", "opening_balance", "adjustment"}:
            raise ReturnError(f"Unknown trade type {self.kind!r}.")
        if self.quantity == ZERO or (self.quantity < ZERO and self.kind != "adjustment"):
            raise ReturnError("Quantity must be positive, or non-zero for an adjustment.")
        if self.price < ZERO or self.fees < ZERO:
            raise ReturnError("Price and fees must be non-negative.")
        _validate_fx(self.fx)

    @property
    def delta(self) -> Decimal:
        """Signed change in units held."""
        return -self.quantity if self.kind == "sell" else self.quantity


@dataclass(frozen=True)
class SplitEvent:
    """`before` old units became `after` new units on `day`."""

    day: date
    before: Decimal
    after: Decimal
    automatic: bool = False

    def __post_init__(self) -> None:
        for value in (self.before, self.after):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise ReturnError("Split ratios must be finite positive Decimals.")


def _validate_fx(value: Decimal | None) -> None:
    if value is not None and (
        not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
    ):
        raise ReturnError("FX must be a finite positive Decimal.")


@dataclass(frozen=True)
class Payment:
    day: date
    amount: Decimal
    currency: str
    fx: Decimal | None = None
    gross: Decimal | None = None
    withholding_tax: Decimal = ZERO
    imputation_credits: Decimal = ZERO

    def __post_init__(self) -> None:
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount < ZERO
        ):
            raise ReturnError("Income must be a finite non-negative Decimal.")
        for value in (self.gross, self.withholding_tax, self.imputation_credits):
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            ):
                raise ReturnError("Income tax fields must be finite non-negative Decimals.")
        _validate_fx(self.fx)


@dataclass(frozen=True)
class Holding:
    exchange: str
    code: str
    currency: str
    trades: tuple[Transaction, ...] = ()
    prices: tuple[Observation, ...] = ()
    income: tuple[Payment, ...] = ()
    splits: tuple[SplitEvent, ...] = ()


@dataclass(frozen=True)
class Components:
    capital: Decimal = ZERO
    dividends: Decimal = ZERO
    currency: Decimal = ZERO
    imputation: Decimal = ZERO

    @property
    @_financial_math
    def total(self) -> Decimal:
        return self.capital + self.dividends + self.currency + self.imputation


@dataclass(frozen=True)
class Result:
    label: str
    opening: Decimal
    closing: Decimal
    purchases: Decimal
    sales: Decimal
    amounts: Components
    flags: tuple[str, ...] = ()
    exchange: str | None = None
    # Net non-cash value moved in (positive) or out by adjustments, at recorded prices.
    adjustments: Decimal = ZERO
    gross_dividends: Decimal = ZERO
    withholding_tax: Decimal = ZERO
    imputation_gross_up: Decimal = ZERO

    @property
    @_financial_math
    def denominator(self) -> Decimal:
        return self.opening + self.purchases

    @property
    @_financial_math
    def percentages(self) -> Components | None:
        if self.denominator == ZERO:
            return None
        factor = HUNDRED / self.denominator
        return Components(
            self.amounts.capital * factor,
            self.amounts.dividends * factor,
            self.amounts.currency * factor,
            self.amounts.imputation * factor,
        )


@dataclass(frozen=True)
class Report:
    start: date
    end: date
    base_currency: str
    holdings: tuple[Result, ...]
    markets: tuple[Result, ...]
    total: Result


def period_range(period: str, today: date, first_trade: date | None = None) -> tuple[date, date]:
    """Named periods end today; start is an opening close, except 'all'."""
    if period == "week":
        return today - timedelta(days=7), today
    if period == "month":
        import calendar

        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return date(year, month, min(today.day, calendar.monthrange(year, month)[1])), today
    if period == "ytd":
        return date(today.year - 1, 12, 31), today
    if period == "all":
        return (first_trade - timedelta(days=1) if first_trade else today), today
    try:
        start, end = (date.fromisoformat(part) for part in period.split(":"))
    except ValueError:
        raise ReturnError(
            "Period must be week, month, ytd, all or YYYY-MM-DD:YYYY-MM-DD."
        ) from None
    if start > end:
        raise ReturnError("Period start must not be after its end.")
    return start, end


def _skips_trading_day(calendar: TradingCalendar, source: date, day: date) -> bool:
    """True when the market traded after `source` and on or before `day`."""
    current = source + timedelta(days=1)
    while current <= day:
        if calendar.is_trading_day(current):
            return True
        current += timedelta(days=1)
    return False


def _lookup(
    points: tuple[Observation, ...],
    day: date,
    label: str,
    flags: set[str],
    calendar: TradingCalendar,
) -> Decimal:
    """Latest observation on or before `day`.

    Flagged when stored as stale, or when a trading day passed without a fresh one.
    Using Friday's close on a weekend or a market holiday is normal and not flagged.
    """
    point = max((p for p in points if p.day <= day), key=lambda p: p.day, default=None)
    if point is None:
        raise ReturnError(f"Missing {label} on or before {day}.")
    if point.stale or _skips_trading_day(calendar, point.day, day):
        flags.add(f"Carried forward {label} for {day} (source {point.day})")
    return point.value


def _ordered_trades(holding: Holding, end: date) -> list[Transaction]:
    """Split once against day-opening units, then additions before removals.

    Dates have no execution times. Ties keep input order (database ID in the
    loader), defining FIFO among same-day additions deterministically.
    """
    splits = {s.day: s for s in holding.splits}
    ordered = []
    held = ZERO
    daily_trades = {
        day: list(group)
        for day, group in groupby(
            sorted((t for t in holding.trades if t.day <= end), key=lambda t: t.day),
            key=lambda t: t.day,
        )
    }
    days = set(daily_trades)
    days.update(s.day for s in holding.splits if s.automatic and s.day <= end)
    for day in sorted(days):
        daily = daily_trades.get(day, [])
        split = splits.get(day)
        if split is not None and split.automatic and held:
            change = held * (split.after - split.before) / split.before
            if not any(
                t.kind == "adjustment" and _matches_split(held, t.quantity, split) for t in daily
            ):
                daily.insert(0, Transaction(day, "adjustment", change, ZERO))
        matching = next(
            (
                i
                for i, trade in enumerate(daily)
                if trade.kind == "adjustment"
                and split is not None
                and _matches_split(held, trade.quantity, split)
            ),
            None,
        )
        held += sum((t.delta for t in daily), ZERO)
        if matching is not None:
            ordered.append(daily.pop(matching))
        ordered.extend(sorted(daily, key=lambda t: t.delta < ZERO))
    return ordered


def _opening_quantity(holding: Holding, trades: list[Transaction], start: date) -> Decimal:
    """Units held at the opening close; every trade type, adjustments included, counts."""
    quantity = ZERO
    for trade in trades:
        if trade.day > start:
            break
        quantity += trade.delta
        if quantity < ZERO:
            raise ReturnError(f"{holding.code}: sale exceeds held quantity.")
    return quantity


def _matches_split(held: Decimal, change: Decimal, split: SplitEvent) -> bool:
    """Match the recorded ratio exactly, without rounding a transfer into a split."""
    if held <= ZERO:
        return False
    return change * split.before == held * (split.after - split.before)


@_financial_math
def holding_return(
    holding: Holding,
    start: date,
    end: date,
    rates: dict[str, tuple[Observation, ...]],
    base_currency: str = "NZD",
    *,
    gross_up: bool = False,
) -> Result:
    if start > end:
        raise ReturnError("Period start must not be after its end.")
    flags: set[str] = set()

    calendar = for_exchange(holding.exchange)

    def fx(currency: str, day: date, explicit: Decimal | None = None) -> Decimal:
        if currency == base_currency:
            if explicit is not None and explicit != Decimal(1):
                raise ReturnError("Base-currency FX must be one.")
            return Decimal(1)
        value = (
            explicit
            if explicit is not None
            else _lookup(rates.get(currency, ()), day, f"{currency}/{base_currency} FX", flags, FX)
        )
        if value <= ZERO:
            raise ReturnError("FX must be positive.")
        return value

    def price_on(day: date) -> Decimal:
        close = _lookup(holding.prices, day, f"{holding.code} price", flags, calendar)
        source = max(p.day for p in holding.prices if p.day <= day)
        for split in holding.splits:
            if split.automatic and source < split.day <= day:
                close *= split.before / split.after
        return close

    trades = _ordered_trades(holding, end)
    quantity = _opening_quantity(holding, trades, start)
    splits = {s.day: s for s in holding.splits}
    # A parcel is [units, local cost, entry FX]. Reset history at opening close.
    parcels: list[list[Decimal]] = []
    opening = ZERO
    if quantity:
        price = price_on(start)
        rate = fx(holding.currency, start)
        parcels.append([quantity, quantity * price, rate])
        opening = quantity * price * rate
    capital = currency_gain = purchases = sales = adjustments = ZERO

    def remove(units: Decimal, p1: Decimal, f1: Decimal) -> None:
        """Realise `units` from the oldest parcels at local price p1 and FX f1."""
        nonlocal capital, currency_gain
        if units > quantity:
            raise ReturnError(f"{holding.code}: sale exceeds held quantity.")
        while units:
            parcel = parcels[0]
            taken = min(units, parcel[0])
            cost = parcel[1] if taken == parcel[0] else parcel[1] * taken / parcel[0]
            capital += (taken * p1 - cost) * parcel[2]
            currency_gain += taken * p1 * (f1 - parcel[2])
            parcel[0] -= taken
            parcel[1] -= cost
            units -= taken
            if not parcel[0]:
                parcels.pop(0)

    seen_days: set[date] = set()
    for trade in trades:
        if trade.day <= start:
            continue
        split = splits.get(trade.day)
        first_on_day = trade.day not in seen_days
        seen_days.add(trade.day)
        if (
            trade.kind == "adjustment"
            and split is not None
            and first_on_day
            and _matches_split(quantity, trade.quantity, split)
        ):
            # Preserve local cost and entry FX. A zero-fee split needs no FX quote.
            if trade.fees:
                fee = trade.fees * fx(holding.currency, trade.day, trade.fx)
                capital -= fee
                purchases += fee
            new = quantity + trade.quantity
            for parcel in parcels[:-1]:
                parcel[0] = parcel[0] * new / quantity
            parcels[-1][0] = new - sum((p[0] for p in parcels[:-1]), ZERO)
            quantity = new
            continue
        rate = fx(holding.currency, trade.day, trade.fx)
        fee = trade.fees * rate
        capital -= fee
        if trade.kind == "sell":
            remove(trade.quantity, trade.price, rate)
            sales += trade.quantity * trade.price * rate - fee
        elif trade.kind != "adjustment":
            purchases += trade.quantity * trade.price * rate + fee
            parcels.append([trade.quantity, trade.quantity * trade.price, rate])
        else:
            # Adjustment fees are cash paid, so they count as an outlay.
            purchases += fee
            if trade.quantity > ZERO:
                adjustments += trade.quantity * trade.price * rate
                parcels.append([trade.quantity, trade.quantity * trade.price, rate])
            else:
                remove(-trade.quantity, trade.price, rate)
                adjustments += trade.quantity * trade.price * rate
        quantity += trade.delta
    closing = ZERO
    if quantity:
        price = price_on(end)
        rate = fx(holding.currency, end)
        closing = quantity * price * rate
        remove(quantity, price, rate)
    dividends = gross = tax = credits = ZERO
    for payment in holding.income:
        if start < payment.day <= end:
            rate = fx(payment.currency, payment.day, payment.fx)
            dividends += payment.amount * rate
            gross += (payment.gross if payment.gross is not None else payment.amount) * rate
            tax += payment.withholding_tax * rate
            credits += payment.imputation_credits * rate

    return Result(
        f"{holding.exchange}:{holding.code}",
        opening,
        closing,
        purchases,
        sales,
        Components(capital, dividends, currency_gain, credits if gross_up else ZERO),
        tuple(sorted(flags)),
        holding.exchange,
        adjustments,
        gross,
        tax,
        credits,
    )


@_financial_math
def subtotal(label: str, rows: tuple[Result, ...]) -> Result:
    return Result(
        label,
        sum((r.opening for r in rows), ZERO),
        sum((r.closing for r in rows), ZERO),
        sum((r.purchases for r in rows), ZERO),
        sum((r.sales for r in rows), ZERO),
        Components(
            sum((r.amounts.capital for r in rows), ZERO),
            sum((r.amounts.dividends for r in rows), ZERO),
            sum((r.amounts.currency for r in rows), ZERO),
            sum((r.amounts.imputation for r in rows), ZERO),
        ),
        tuple(sorted({flag for r in rows for flag in r.flags})),
        adjustments=sum((r.adjustments for r in rows), ZERO),
        gross_dividends=sum((r.gross_dividends for r in rows), ZERO),
        withholding_tax=sum((r.withholding_tax for r in rows), ZERO),
        imputation_gross_up=sum((r.imputation_gross_up for r in rows), ZERO),
    )


def _active(holding: Holding, start: date, end: date) -> bool:
    """Held at the opening close, traded in the period, or paid income in it."""
    return (
        any(start < t.day <= end for t in holding.trades)
        or any(start < p.day <= end for p in holding.income)
        or _opening_quantity(holding, _ordered_trades(holding, start), start) != ZERO
    )


@_financial_math
def calculate_returns(
    holdings: tuple[Holding, ...],
    start: date,
    end: date,
    rates: dict[str, tuple[Observation, ...]],
    base_currency: str = "NZD",
    *,
    gross_up: bool = False,
) -> Report:
    if start > end:
        raise ReturnError("Period start must not be after its end.")
    # Holdings sold before the period have nothing to report, so they are left out.
    ordered = sorted(
        (h for h in holdings if _active(h, start, end)), key=lambda h: (h.exchange, h.code)
    )
    rows = tuple(
        holding_return(h, start, end, rates, base_currency, gross_up=gross_up) for h in ordered
    )
    markets = tuple(
        subtotal(
            market, tuple(r for h, r in zip(ordered, rows, strict=True) if h.exchange == market)
        )
        for market in sorted({h.exchange for h in ordered})
    )
    return Report(start, end, base_currency, rows, markets, subtotal("Portfolio", rows))


@dataclass
class Position:
    units: Decimal = ZERO
    cost_base: Decimal = ZERO
    purchases: Decimal = ZERO
    proceeds: Decimal = ZERO


def _ledger_events(trades, splits, end):
    split_rows = list(splits)
    transactions = []
    originals = {}
    for trade in trades:
        transaction = Transaction(
            trade.trade_date, trade.trade_type, trade.quantity, trade.price, trade.fees
        )
        transactions.append(transaction)
        originals[id(transaction)] = trade
    holding = Holding(
        "",
        "",
        "",
        tuple(transactions),
        splits=tuple(
            SplitEvent(
                s.split_date,
                s.shares_before,
                s.shares_after,
                getattr(s, "source", "registry") == "registry",
            )
            for s in split_rows
        ),
    )
    return _ordered_trades(holding, end), {s.day: s for s in holding.splits}, originals


@_financial_math
def units_at(trades, splits, end: date) -> Decimal:
    """Quantity ledger shared by reconciliation, fetching and dividend suggestions."""
    ordered, _, _ = _ledger_events(trades, splits, end)
    return sum((trade.delta for trade in ordered), ZERO)


@_financial_math
def position(trades, splits, end: date, *, before_trades: bool = False) -> Position:
    """Replay dated trades and registry actions without counting splits twice."""
    trades = list(trades)
    if before_trades:
        trades = [t for t in trades if t.trade_date < end]
    ordered, split_days, originals = _ledger_events(trades, splits, end)
    result = Position()
    seen_days = set()
    for row in ordered:
        split = split_days.get(row.day)
        first = row.day not in seen_days
        seen_days.add(row.day)
        if (
            first
            and row.kind == "adjustment"
            and split is not None
            and _matches_split(result.units, row.quantity, split)
        ):
            result.units += row.quantity
            result.cost_base += row.fees
            result.purchases += row.fees
            continue
        original = originals.get(id(row))
        amount = (
            original.consideration
            if original is not None and original.consideration is not None
            else row.quantity * row.price
        )
        if row.kind == "sell" or row.quantity < 0:
            removed = abs(row.quantity)
            if removed > result.units:
                raise ImportFailure(
                    "Units removed exceed holdings; check trades and corporate actions."
                )
            result.cost_base -= result.cost_base * removed / result.units
            result.units -= removed
            result.proceeds += amount - row.fees if row.kind == "sell" else ZERO
        else:
            result.units += row.quantity
            result.cost_base += amount + row.fees
            result.purchases += amount + row.fees
    return result


def instrument_position(
    session: Session,
    portfolio_id: int,
    instrument_id: int,
    end: date,
    *,
    before_trades: bool = False,
) -> Position:
    return position(
        session.scalars(
            select(Trade)
            .where(Trade.portfolio_id == portfolio_id, Trade.instrument_id == instrument_id)
            .order_by(Trade.trade_date, Trade.id)
        ),
        session.scalars(select(Split).where(Split.instrument_id == instrument_id)),
        end,
        before_trades=before_trades,
    )


def fx(session, currency, base, day, broker_rate=None):
    if currency == base:
        return Decimal(1)
    if broker_rate is not None:
        return broker_rate
    rate = session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.currency == currency,
            FxRate.base_currency == base,
            FxRate.rate_date <= day,
        )
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    )
    if rate is None:
        raise ImportFailure(f"Missing {currency}/{base} exchange rate on or before {day}.")
    return rate


@dataclass(frozen=True)
class ReturnRow:
    instrument: str
    currency: str
    units: Decimal
    cost_base: Decimal
    market_value: Decimal
    dividends: Decimal
    gross_dividends: Decimal
    withholding_tax: Decimal
    imputation_gross_up: Decimal
    total_return: Decimal


@_financial_math
def report(
    session: Session, portfolio: Portfolio, end: date, *, gross_up: bool = False
) -> list[ReturnRow]:
    """Base-currency lifetime gain: value + sale proceeds + income - purchases.

    No percentage is implied where the timing of contributions is unknown. Cost base
    is a weighted-average accounting cost in base currency, not a tax calculation.
    """
    ids = set(
        session.scalars(
            select(Trade.instrument_id).where(
                Trade.portfolio_id == portfolio.id, Trade.trade_date <= end
            )
        )
    )
    ids.update(
        session.scalars(
            select(Income.instrument_id).where(
                Income.portfolio_id == portfolio.id, Income.paid_date <= end
            )
        )
    )
    rows = []
    for instrument_id in sorted(ids):
        instrument = session.get(Instrument, instrument_id)
        trades = list(
            session.scalars(
                select(Trade)
                .where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.instrument_id == instrument_id,
                    Trade.trade_date <= end,
                )
                .order_by(Trade.trade_date, Trade.id)
            )
        )
        # Convert cash flows at their own dates before replaying the cost ledger.
        from types import SimpleNamespace

        converted = []
        for trade in trades:
            rate = fx(
                session, trade.currency, portfolio.base_currency, trade.trade_date, trade.fx_rate
            )
            converted.append(
                SimpleNamespace(
                    trade_date=trade.trade_date,
                    trade_type=trade.trade_type,
                    quantity=trade.quantity,
                    price=trade.price * rate,
                    fees=trade.fees * rate,
                    consideration=trade.consideration * rate
                    if trade.consideration is not None
                    else None,
                )
            )
        splits = list(session.scalars(select(Split).where(Split.instrument_id == instrument_id)))
        holding = position(converted, splits, end)
        value = ZERO
        if holding.units:
            latest = session.scalar(
                select(Price)
                .where(Price.instrument_id == instrument_id, Price.price_date <= end)
                .order_by(Price.price_date.desc())
                .limit(1)
            )
            if latest is None:
                raise ImportFailure(f"Missing price for {instrument.code} on or before {end}.")
            close = latest.close
            for split in splits:
                if split.source == "registry" and latest.price_date < split.split_date <= end:
                    close *= split.shares_before / split.shares_after
            value = (
                holding.units
                * close
                * fx(session, instrument.currency, portfolio.base_currency, end)
            )
        net = gross = tax = credits = ZERO
        for income in session.scalars(
            select(Income).where(
                Income.portfolio_id == portfolio.id,
                Income.instrument_id == instrument_id,
                Income.paid_date <= end,
            )
        ):
            rate = fx(
                session, income.currency, portfolio.base_currency, income.paid_date, income.fx_rate
            )
            net += income.net_amount * rate
            gross += income.gross_amount * rate
            tax += income.withholding_tax * rate
            credits += income.imputation_credits * rate
        rows.append(
            ReturnRow(
                f"{instrument.exchange}:{instrument.code}",
                portfolio.base_currency,
                holding.units,
                holding.cost_base,
                value,
                net,
                gross,
                tax,
                credits,
                value
                + holding.proceeds
                + net
                - holding.purchases
                + (credits if gross_up else ZERO),
            )
        )
    return rows
