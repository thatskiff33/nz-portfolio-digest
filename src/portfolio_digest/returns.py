"""Exact Decimal period performance and quantity ledger helpers.

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

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importing import ImportFailure
from portfolio_digest.models import Split, Trade
from portfolio_digest.parcels import (
    DatedFx,
    Observation,
    ParcelLedger,
    ReturnError,
    financial_math,
    lookup,
)
from portfolio_digest.prices.calendars import for_exchange

ZERO = Decimal(0)
HUNDRED = Decimal(100)


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
    name: str = ""


@dataclass(frozen=True)
class Components:
    capital: Decimal = ZERO
    dividends: Decimal = ZERO
    currency: Decimal = ZERO
    imputation: Decimal = ZERO

    @property
    @financial_math
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
    units: Decimal = ZERO
    price: Decimal | None = None
    currency: str = ""
    name: str = ""

    @property
    @financial_math
    def change(self) -> Decimal:
        return self.closing - self.opening

    @property
    @financial_math
    def denominator(self) -> Decimal:
        return self.opening + self.purchases

    @property
    @financial_math
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


def ordered_trades(holding: Holding, end: date) -> list[Transaction]:
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
                t.kind == "adjustment" and matches_split(held, t.quantity, split) for t in daily
            ):
                daily.insert(0, Transaction(day, "adjustment", change, ZERO))
        matching = next(
            (
                i
                for i, trade in enumerate(daily)
                if trade.kind == "adjustment"
                and split is not None
                and matches_split(held, trade.quantity, split)
            ),
            None,
        )
        held += sum((t.delta for t in daily), ZERO)
        if matching is not None:
            ordered.append(daily.pop(matching))
        ordered.extend(sorted(daily, key=lambda t: t.delta < ZERO))
    return ordered


@financial_math
def holding_units_at(holding: Holding, end: date) -> Decimal:
    """Closing units from loaded inputs, including registry and recorded splits."""
    return sum((trade.delta for trade in ordered_trades(holding, end)), ZERO)


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


def matches_split(held: Decimal, change: Decimal, split: SplitEvent) -> bool:
    """Match the recorded ratio exactly, without rounding a transfer into a split."""
    if held <= ZERO:
        return False
    return change * split.before == held * (split.after - split.before)


@financial_math
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

    fx = DatedFx(rates, base_currency, flags)

    def price_on(day: date) -> Decimal:
        close = lookup(holding.prices, day, f"{holding.code} price", flags, calendar)
        source = max(p.day for p in holding.prices if p.day <= day)
        for split in holding.splits:
            if split.automatic and source < split.day <= day:
                close *= split.before / split.after
        return close

    trades = ordered_trades(holding, end)
    quantity = _opening_quantity(holding, trades, start)
    splits = {s.day: s for s in holding.splits}
    # Reset performance history at the opening close.
    parcels = ParcelLedger(holding.code)
    opening = ZERO
    if quantity:
        price = price_on(start)
        rate = fx(holding.currency, start)
        parcels.add(quantity, quantity * price, rate)
        opening = quantity * price * rate
    capital = currency_gain = purchases = sales = adjustments = ZERO

    def remove(units: Decimal, p1: Decimal, f1: Decimal) -> None:
        """Realise `units` from the oldest parcels at local price p1 and FX f1."""
        nonlocal capital, currency_gain
        for parcel in parcels.remove(units):
            capital += (parcel.units * p1 - parcel.cost) * parcel.fx
            currency_gain += parcel.units * p1 * (f1 - parcel.fx)

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
            and matches_split(quantity, trade.quantity, split)
        ):
            # Preserve local cost and entry FX. A zero-fee split needs no FX quote.
            if trade.fees:
                fee = trade.fees * fx(holding.currency, trade.day, trade.fx)
                capital -= fee
                purchases += fee
            new = quantity + trade.quantity
            parcels.rescale(new)
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
            parcels.add(trade.quantity, trade.quantity * trade.price, rate)
        else:
            # Adjustment fees are cash paid, so they count as an outlay.
            purchases += fee
            if trade.quantity > ZERO:
                adjustments += trade.quantity * trade.price * rate
                parcels.add(trade.quantity, trade.quantity * trade.price, rate)
            else:
                remove(-trade.quantity, trade.price, rate)
                adjustments += trade.quantity * trade.price * rate
        quantity += trade.delta
    closing = ZERO
    price = None
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
        quantity,
        price,
        holding.currency,
        holding.name,
    )


@financial_math
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
        units=sum((r.units for r in rows), ZERO),
    )


def _active(holding: Holding, start: date, end: date) -> bool:
    """Held at the opening close, traded in the period, or paid income in it."""
    return (
        any(start < t.day <= end for t in holding.trades)
        or any(start < p.day <= end for p in holding.income)
        or _opening_quantity(holding, ordered_trades(holding, start), start) != ZERO
    )


@financial_math
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
    return ordered_trades(holding, end), {s.day: s for s in holding.splits}, originals


@financial_math
def units_at(trades, splits, end: date) -> Decimal:
    """Quantity ledger shared by reconciliation, fetching and dividend suggestions."""
    ordered, _, _ = _ledger_events(trades, splits, end)
    return sum((trade.delta for trade in ordered), ZERO)


@financial_math
def position(trades, splits, end: date, *, before_trades: bool = False) -> Position:
    """Replay dated trades and registry actions without counting splits twice."""
    trades = list(trades)
    if before_trades:
        trades = [t for t in trades if t.trade_date < end]
    ordered, split_days, originals = _ledger_events(trades, splits, end)
    result = Position()
    ledger = ParcelLedger("Holding")
    seen_days = set()
    for row in ordered:
        split = split_days.get(row.day)
        first = row.day not in seen_days
        seen_days.add(row.day)
        if (
            first
            and row.kind == "adjustment"
            and split is not None
            and matches_split(result.units, row.quantity, split)
        ):
            ledger.rescale(result.units + row.quantity, row.fees)
            result.units = ledger.units
            result.cost_base = ledger.cost
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
            ledger.remove(removed)
            result.cost_base = ledger.cost
            result.units -= removed
            result.proceeds += amount - row.fees if row.kind == "sell" else ZERO
        else:
            result.units += row.quantity
            ledger.add(row.quantity, amount + row.fees)
            result.cost_base = ledger.cost
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
