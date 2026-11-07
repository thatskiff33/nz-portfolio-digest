"""Public exact parcel operations and dated market-data lookup."""

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from functools import wraps
from typing import ParamSpec, TypeVar

from portfolio_digest.models import FINANCIAL_PRECISION
from portfolio_digest.prices.calendars import FX, TradingCalendar

ZERO = Decimal(0)
P = ParamSpec("P")
T = TypeVar("T")


def financial_math(function: Callable[P, T]) -> Callable[P, T]:
    """Allow 64-digit schema products plus subtraction and aggregation headroom."""

    @wraps(function)
    def exact(*args: P.args, **kwargs: P.kwargs) -> T:
        with localcontext() as context:
            context.prec = max(context.prec, FINANCIAL_PRECISION)
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


class ObservationSeries(tuple):
    """Stable date ordering prepared once for logarithmic lookups."""

    def __new__(cls, points=()):
        if isinstance(points, cls):
            return points
        return super().__new__(cls, sorted(points, key=lambda p: p.day))


def _require_aware(as_of: datetime | None) -> datetime | None:
    """Reject a clock time that does not say which zone it belongs to."""
    if as_of is not None and (as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None):
        raise ReturnError("Valuation instant must include a timezone.")
    return as_of


def _closed_day(calendar: TradingCalendar, day: date, as_of: datetime | None) -> date:
    """Last day whose close counts as missing.

    With no instant, the requested date itself counts: date-only reports never read
    the clock. With an instant, a session that has not closed yet is not missing.
    """
    if as_of is None:
        return day
    _require_aware(as_of)
    return min(day, calendar.latest_closed_day(as_of))


def _skips_trading_day(
    calendar: TradingCalendar, source: date, day: date, *, as_of: datetime | None = None
) -> bool:
    """True when the market traded after `source` and on or before the last due close."""
    limit = _closed_day(calendar, day, as_of)
    current = source + timedelta(days=1)
    while current <= limit:
        if calendar.is_trading_day(current):
            return True
        current += timedelta(days=1)
    return False


def lookup(
    points: tuple[Observation, ...],
    day: date,
    label: str,
    flags: set[str],
    calendar: TradingCalendar,
    *,
    as_of: datetime | None = None,
) -> Decimal:
    """Latest observation on or before `day`.

    Flagged when stored as stale, or when a trading day whose close was already due
    passed without a fresh one. Using Friday's close on a weekend or a market holiday
    is normal and not flagged. The observation itself is still chosen on or before
    the requested date; `as_of` only limits which missing sessions count.
    """
    if not isinstance(points, ObservationSeries):
        points = ObservationSeries(points)
    index = bisect_right(points, day, key=lambda p: p.day) - 1
    # Keep the original first-observation rule for duplicate dates.
    point = (
        points[bisect_left(points, points[index].day, key=lambda p: p.day)] if index >= 0 else None
    )
    if point is None:
        raise ReturnError(f"Missing {label} on or before {day}.")
    if point.stale or _skips_trading_day(calendar, point.day, day, as_of=as_of):
        flags.add(f"Carried forward {label} for {day} (source {point.day})")
    return point.value


@dataclass
class Parcel:
    units: Decimal
    cost: Decimal
    fx: Decimal


@dataclass
class ParcelLedger:
    """FIFO units, local cost and entry FX; callers choose their fee treatment.

    Cost is local for performance parcels and NZD for historical tax parcels.
    All arithmetic runs in the shared financial context of at least 80 digits.
    """

    label: str
    _lots: list[Parcel] = field(default_factory=list, init=False, repr=False)

    @property
    @financial_math
    def units(self) -> Decimal:
        return sum((lot.units for lot in self._lots), ZERO)

    @property
    @financial_math
    def cost(self) -> Decimal:
        return sum((lot.cost for lot in self._lots), ZERO)

    def add(self, units: Decimal, cost: Decimal, fx: Decimal = Decimal(1)) -> None:
        if not units.is_finite() or units <= ZERO:
            raise ReturnError("Parcel units must be positive and finite.")
        self._lots.append(Parcel(units, cost, fx))

    @financial_math
    def remove(self, units: Decimal) -> list[Parcel]:
        if not units.is_finite() or units < ZERO:
            raise ReturnError("Removed units must be non-negative and finite.")
        if units > self.units:
            raise ReturnError(f"{self.label}: sale exceeds held quantity.")
        removed = []
        while units:
            lot = self._lots[0]
            taken = min(units, lot.units)
            cost = lot.cost if taken == lot.units else lot.cost * taken / lot.units
            removed.append(Parcel(taken, cost, lot.fx))
            lot.units -= taken
            lot.cost -= cost
            units -= taken
            if not lot.units:
                self._lots.pop(0)
        return removed

    @financial_math
    def rescale(self, new: Decimal, fee: Decimal = ZERO) -> None:
        held = self.units
        if not held or not new.is_finite() or new <= ZERO:
            raise ReturnError("A split requires positive existing and resulting units.")
        for lot in self._lots[:-1]:
            lot.units = lot.units * new / held
        self._lots[-1].units = new - sum((lot.units for lot in self._lots[:-1]), ZERO)
        # Allocate historical split fees by units, keeping the remainder exact.
        remaining = fee
        for lot in self._lots[:-1]:
            share = fee * lot.units / new
            lot.cost += share
            remaining -= share
        self._lots[-1].cost += remaining


@dataclass
class DatedFx:
    rates: dict[str, tuple[Observation, ...]]
    base_currency: str
    flags: set[str]
    as_of: datetime | None = None

    def __call__(self, currency: str, day: date, explicit: Decimal | None = None) -> Decimal:
        if currency == self.base_currency:
            if explicit is not None and explicit != Decimal(1):
                raise ReturnError("Base-currency FX must be one.")
            return Decimal(1)
        value = (
            explicit
            if explicit is not None
            else lookup(
                self.rates.get(currency, ()),
                day,
                f"{currency}/{self.base_currency} FX",
                self.flags,
                FX,
                as_of=self.as_of,
            )
        )
        if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
            raise ReturnError("FX must be positive and finite.")
        return value
