"""Pure period performance, with exact Decimal arithmetic and no database access.

Each opening or purchased FIFO parcel has quantity q, local entry price p0 and
entry FX f0 (base currency per local unit). On sale or closing valuation p1, f1:
capital = q * (p1 - p0) * f0; currency = q * p1 * (f1 - f0).
The cross-term belongs to currency. Their sum is q * (p1*f1 - p0*f0).
Fees reduce capital. All percentages share opening value plus purchase outlays
as denominator. Sales are withdrawals, not a reduction in capital put at risk.
See docs/RETURNS.md for boundaries, income and missing-data conventions.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal(0)
HUNDRED = Decimal(100)


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
        if self.quantity <= ZERO or self.price < ZERO or self.fees < ZERO:
            raise ReturnError("Quantity must be positive; price and fees non-negative.")
        if self.kind not in {"buy", "sell", "opening_balance"}:
            raise ReturnError("Adjustments need explicit corporate-action handling before returns.")
        _validate_fx(self.fx)


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


@dataclass(frozen=True)
class Components:
    capital: Decimal = ZERO
    dividends: Decimal = ZERO
    currency: Decimal = ZERO

    @property
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

    @property
    def denominator(self) -> Decimal:
        return self.opening + self.purchases

    @property
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


def _lookup(points: tuple[Observation, ...], day: date, label: str, flags: set[str]) -> Decimal:
    point = max((p for p in points if p.day <= day), key=lambda p: p.day, default=None)
    if point is None:
        raise ReturnError(f"Missing {label} on or before {day}.")
    if point.stale or point.day < day:
        flags.add(f"Carried forward {label} for {day} (source {point.day})")
    return point.value


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

    def fx(currency: str, day: date, explicit: Decimal | None = None) -> Decimal:
        if currency == base_currency:
            if explicit is not None and explicit != Decimal(1):
                raise ReturnError("Base-currency FX must be one.")
            return Decimal(1)
        value = (
            explicit
            if explicit is not None
            else _lookup(rates.get(currency, ()), day, f"{currency}/{base_currency} FX", flags)
        )
        if value <= ZERO:
            raise ReturnError("FX must be positive.")
        return value

    trades = sorted((t for t in holding.trades if t.day <= end), key=lambda t: t.day)
    quantity = ZERO
    for trade in trades:
        if trade.day > start:
            break
        quantity += -trade.quantity if trade.kind == "sell" else trade.quantity
        if quantity < ZERO:
            raise ReturnError(f"{holding.code}: sale exceeds held quantity.")
    # A parcel is [quantity, local entry price, entry FX]. Reset history at opening close.
    parcels: list[list[Decimal]] = []
    opening = ZERO
    if quantity:
        price = _lookup(holding.prices, start, f"{holding.code} price", flags)
        rate = fx(holding.currency, start)
        parcels.append([quantity, price, rate])
        opening = quantity * price * rate
    capital = currency_gain = purchases = sales = ZERO

    def realise(q: Decimal, p0: Decimal, f0: Decimal, p1: Decimal, f1: Decimal) -> None:
        nonlocal capital, currency_gain
        capital += q * (p1 - p0) * f0
        currency_gain += q * p1 * (f1 - f0)

    for trade in trades:
        if trade.day <= start:
            continue
        rate = fx(holding.currency, trade.day, trade.fx)
        fee = trade.fees * rate
        capital -= fee
        if trade.kind != "sell":
            purchases += trade.quantity * trade.price * rate + fee
            parcels.append([trade.quantity, trade.price, rate])
            quantity += trade.quantity
        else:
            if trade.quantity > quantity:
                raise ReturnError(f"{holding.code}: sale exceeds held quantity.")
            sales += trade.quantity * trade.price * rate - fee
            remaining = trade.quantity
            while remaining:
                parcel = parcels[0]
                taken = min(remaining, parcel[0])
                realise(taken, parcel[1], parcel[2], trade.price, rate)
                parcel[0] -= taken
                remaining -= taken
                if not parcel[0]:
                    parcels.pop(0)
            quantity -= trade.quantity
    closing = ZERO
    if quantity:
        price = _lookup(holding.prices, end, f"{holding.code} price", flags)
        rate = fx(holding.currency, end)
        closing = quantity * price * rate
        for q, p0, f0 in parcels:
            realise(q, p0, f0, price, rate)
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
    )


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
    )


def calculate_returns(
    holdings: tuple[Holding, ...],
    start: date,
    end: date,
    rates: dict[str, tuple[Observation, ...]],
    base_currency: str = "NZD",
) -> Report:
    if start > end:
        raise ReturnError("Period start must not be after its end.")
    ordered = sorted(holdings, key=lambda h: (h.exchange, h.code))
    rows = tuple(holding_return(h, start, end, rates, base_currency) for h in ordered)
    markets = tuple(
        subtotal(
            market, tuple(r for h, r in zip(ordered, rows, strict=True) if h.exchange == market)
        )
        for market in sorted({h.exchange for h in ordered})
    )
    return Report(start, end, base_currency, rows, markets, subtotal("Portfolio", rows))
