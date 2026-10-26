"""Pure period performance, with exact Decimal arithmetic and no database access.

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

    def __post_init__(self) -> None:
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount < ZERO
        ):
            raise ReturnError("Income must be a finite non-negative Decimal.")
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

    @property
    @_financial_math
    def total(self) -> Decimal:
        return self.capital + self.dividends + self.currency


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
    for day, group in groupby(
        sorted((t for t in holding.trades if t.day <= end), key=lambda t: t.day),
        key=lambda t: t.day,
    ):
        daily = list(group)
        split = splits.get(day)
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
        return _lookup(holding.prices, day, f"{holding.code} price", flags, calendar)

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
    dividends = sum(
        (p.amount * fx(p.currency, p.day, p.fx) for p in holding.income if start < p.day <= end),
        ZERO,
    )
    return Result(
        f"{holding.exchange}:{holding.code}",
        opening,
        closing,
        purchases,
        sales,
        Components(capital, dividends, currency_gain),
        tuple(sorted(flags)),
        holding.exchange,
        adjustments,
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
        ),
        tuple(sorted({flag for r in rows for flag in r.flags})),
        adjustments=sum((r.adjustments for r in rows), ZERO),
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
) -> Report:
    if start > end:
        raise ReturnError("Period start must not be after its end.")
    # Holdings sold before the period have nothing to report, so they are left out.
    ordered = sorted(
        (h for h in holdings if _active(h, start, end)), key=lambda h: (h.exchange, h.code)
    )
    rows = tuple(holding_return(h, start, end, rates, base_currency) for h in ordered)
    markets = tuple(
        subtotal(
            market, tuple(r for h, r in zip(ordered, rows, strict=True) if h.exchange == market)
        )
        for market in sorted({h.exchange for h in ordered})
    )
    return Report(start, end, base_currency, rows, markets, subtotal("Portfolio", rows))
