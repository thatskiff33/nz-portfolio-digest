"""Fetch daily prices and exchange rates into the database.

Every instrument that has trades gets prices from its first trade date until today, or
until the day it was sold down to nothing. Every currency those instruments trade in gets
a daily rate against the base currency (NZD) over the same period.

A normal fetch picks up from the last good price and always re-reads the last month, so a
price Yahoo corrects later, or a day that was carried forward while Yahoo was down, is
fixed on the next run. Re-running a fetch overwrites rows rather than adding new ones.

When a source has nothing for a trading day, the last known price is carried forward and
marked stale. A carried-forward row never replaces a real one.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from portfolio_digest.db import session_scope
from portfolio_digest.models import FxRate, Instrument, Price, Split, Trade
from portfolio_digest.prices import calendars
from portfolio_digest.prices.http import SourceError
from portfolio_digest.prices.sources import FxSource, Listing, PriceSource, _fits_numeric
from portfolio_digest.prices.symbols import UnknownSymbol
from portfolio_digest.returns import units_at

log = logging.getLogger(__name__)

# Every fetch re-reads at least this many days, to pick up corrections and replace
# carried-forward prices once the source has the real one.
REFRESH_DAYS = 31

# Rows per INSERT, well inside Postgres's limit on statement parameters.
_BATCH = 1000


@dataclass(frozen=True)
class Holding:
    """An instrument that has been traded, and the dates it needs prices for."""

    instrument_id: int
    exchange: str
    code: str
    currency: str
    yahoo_symbol: str | None
    first_trade: date
    last_trade: date
    # False once every portfolio has sold out of it; prices then stop at the last trade.
    still_held: bool

    @property
    def label(self) -> str:
        return f"{self.exchange}:{self.code}"

    @property
    def listing(self) -> Listing:
        return Listing(self.exchange, self.code, self.yahoo_symbol)

    @property
    def calendar(self) -> calendars.TradingCalendar:
        return calendars.for_exchange(self.exchange)

    def last_day_needed(self, now: datetime) -> date:
        latest = self.calendar.latest_closed_day(now)
        return latest if self.still_held else min(self.last_trade, latest)


@dataclass(frozen=True)
class CurrencyNeed:
    """A currency held, and the dates its rate against the base currency is needed for."""

    currency: str
    base_currency: str
    first_day: date
    last_day: date
    still_held: bool = True

    @property
    def label(self) -> str:
        return f"{self.currency}/{self.base_currency}"


@dataclass
class Outcome:
    """What happened to one instrument or currency in a fetch."""

    label: str
    source: str = ""
    fetched: int = 0
    carried_forward: int = 0
    error: str | None = None
    # Sources that failed before the one that worked.
    fallbacks: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.error:
            text = f"{self.label}: FAILED, {self.error}"
        else:
            text = f"{self.label}: {self.fetched} from {self.source}"
        if self.carried_forward:
            text += f", {self.carried_forward} carried forward (stale)"
        if self.fallbacks:
            text += f" (after {'; '.join(self.fallbacks)})"
        return text


@dataclass
class FetchReport:
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.error is None for o in self.outcomes)


@dataclass(frozen=True)
class Sources:
    prices: PriceSource
    # Tried in order until one has rates.
    fx: Sequence[FxSource]


def held_instruments(session: Session, only: Iterable[str] | None = None) -> list[Holding]:
    """Every instrument with trades, optionally only those named as `EXCHANGE:CODE`."""
    trades_by_position = {}
    for trade in session.scalars(select(Trade)):
        trades_by_position.setdefault((trade.instrument_id, trade.portfolio_id), []).append(trade)
    splits_by_instrument = {}
    for split in session.scalars(select(Split)):
        splits_by_instrument.setdefault(split.instrument_id, []).append(split)
    active = {
        instrument_id
        for (instrument_id, _), trades in trades_by_position.items()
        if units_at(trades, splits_by_instrument.get(instrument_id, []), date.max)
    }
    stmt = (
        select(
            Instrument,
            func.min(Trade.trade_date),
            func.max(Trade.trade_date),
        )
        .join(Trade, Trade.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.exchange, Instrument.code)
    )
    holdings = [
        Holding(
            instrument_id=inst.id,
            exchange=inst.exchange,
            code=inst.code,
            currency=inst.currency,
            yahoo_symbol=inst.yahoo_symbol,
            first_trade=first,
            last_trade=last,
            still_held=inst.id in active,
        )
        for inst, first, last in session.execute(stmt)
    ]
    if only is None:
        return holdings
    wanted = set(only)
    return [h for h in holdings if h.label in wanted]


def currencies_needed(
    holdings: Iterable[Holding], base_currency: str, now: datetime
) -> list[CurrencyNeed]:
    by_currency: dict[str, list[Holding]] = {}
    for h in holdings:
        if h.currency != base_currency:
            by_currency.setdefault(h.currency, []).append(h)
    latest = calendars.FX.latest_closed_day(now)
    needs = []
    for currency, group in sorted(by_currency.items()):
        last = latest if any(h.still_held for h in group) else max(h.last_trade for h in group)
        needs.append(
            CurrencyNeed(
                currency=currency,
                base_currency=base_currency,
                first_day=min(h.first_trade for h in group),
                last_day=min(last, latest),
                still_held=any(h.still_held for h in group),
            )
        )
    return needs


def carry_forward(
    trading_days: Iterable[date],
    known: Mapping[date, Decimal],
    previous: Decimal | None,
    *,
    splits: Mapping[date, tuple[Decimal, Decimal]] | None = None,
) -> dict[date, Decimal]:
    """Trading days with no known value, each given the last value before it.

    `previous` is the last value from before the first day. Days before any value at all
    stay empty, since there is nothing to carry. Price callers supply splits within the
    range to keep carried raw closes in the units effective on each day.
    """
    stale: dict[date, Decimal] = {}
    days = set(trading_days)
    splits = splits or {}
    for day in sorted(days | set(known) | set(splits)):
        if day in splits and previous is not None:
            before, after = splits[day]
            previous = previous * before / after
        if day in known:
            previous = known[day]
        elif day in days and previous is not None:
            stale[day] = previous
    return stale


def fetch_start(first_needed: date, last_good: date | None, last_needed: date) -> date:
    """Where a normal (not backfill) fetch starts: just after the last good value, but
    never later than a month back, and never before the value was first needed."""
    if last_good is None:
        return first_needed
    return max(
        first_needed,
        min(last_good + timedelta(days=1), last_needed - timedelta(REFRESH_DAYS)),
    )


def run_fetch(
    engine: Engine,
    sources: Sources,
    *,
    now: datetime,
    base_currency: str,
    backfill: bool = False,
    start: date | None = None,
    only: Sequence[str] | None = None,
) -> FetchReport:
    """Fetch prices for every held instrument, then the exchange rates to value them.

    A backfill re-reads everything from `start`, or from each first trade date. Otherwise
    each fetch carries on from the last good value. Each instrument and currency is saved
    as it goes, so one failure never loses the rest.
    """
    report = FetchReport()
    with session_scope(engine) as session:
        holdings = held_instruments(session, only)
    if only:
        found = {h.label for h in holdings}
        for label in only:
            if label not in found:
                report.outcomes.append(Outcome(label, error="not found, or it has no trades"))

    for holding in holdings:
        try:
            with session_scope(engine) as session:
                outcome = _update_instrument(
                    session, sources.prices, holding, now, backfill, start, bool(only)
                )
        except SQLAlchemyError:
            # session_scope rolls back, including failures during commit. Do not expose
            # SQL or bound parameters from the exception in logs or the report.
            outcome = Outcome(holding.label, error="database error; changes rolled back")
            log.warning("%s: %s", outcome.label, outcome.error)
        report.outcomes.append(outcome)
    for need in currencies_needed(holdings, base_currency, now):
        try:
            with session_scope(engine) as session:
                outcome = _update_currency(
                    session, sources.fx, need, backfill, start, now, bool(only)
                )
        except SQLAlchemyError:
            outcome = Outcome(need.label, error="database error; changes rolled back")
            log.warning("%s: %s", outcome.label, outcome.error)
        report.outcomes.append(outcome)
    return report


def _range(
    first_needed: date,
    last_needed: date,
    last_good: date | None,
    backfill: bool,
    start: date | None,
) -> date:
    if backfill:
        return start or first_needed
    return fetch_start(first_needed, last_good, last_needed)


def _update_instrument(
    session: Session,
    source: PriceSource,
    h: Holding,
    now: datetime,
    backfill: bool,
    start: date | None,
    force: bool = False,
) -> Outcome:
    outcome = Outcome(h.label, source=source.name)
    end = h.last_day_needed(now)
    last_good = session.scalar(
        select(func.max(Price.price_date)).where(
            Price.instrument_id == h.instrument_id,
            Price.price_date <= end,
            Price.is_stale.is_(False),
        )
    )
    if (
        not (h.still_held or backfill or start is not None or force)
        and last_good is not None
        and (now.date() - end).days > REFRESH_DAYS
        # A takeover SELL can follow the last close, leaving a carried final value.
        and session.scalar(
            select(Price.price_date).where(
                Price.instrument_id == h.instrument_id,
                Price.price_date == h.calendar.last_trading_day(end),
            )
        )
        is not None
    ):
        return outcome
    begin = _range(h.first_trade, end, last_good, backfill, start)
    if begin > end:
        return outcome

    recorded_splits = {
        day: (before, after)
        for day, before, after in session.execute(
            select(Split.split_date, Split.shares_before, Split.shares_after).where(
                Split.instrument_id == h.instrument_id
            )
        )
    }
    registry_splits = {
        row.split_date: (row.shares_before, row.shares_after)
        for row in session.scalars(
            select(Split).where(Split.instrument_id == h.instrument_id, Split.source == "registry")
        )
    }
    closes: dict[date, Decimal] = {}
    splits: dict[date, tuple[Decimal, Decimal]] = {}
    try:
        symbol = source.symbol(h.listing)
        outcome.label = f"{h.label} ({symbol})"
        first_stored = session.scalar(
            select(func.min(Price.price_date)).where(Price.instrument_id == h.instrument_id)
        )
        full_start = min(h.first_trade, first_stored or h.first_trade, begin)
        history = source.history(symbol, begin, end, currency=h.currency, split_start=full_start)
        if any(
            day in history.splits and history.splits[day] != ratio
            for day, ratio in registry_splits.items()
        ):
            raise SourceError("Yahoo split ratio conflicts with a recorded registry action.")
        if begin > full_start and {
            day: ratio for day, ratio in recorded_splits.items() if day >= full_start
        } != {day: ratio for day, ratio in history.splits.items() if day >= full_start}:
            # Do not reconcile events until the full refresh succeeds.
            history = source.history(
                symbol, full_start, end, currency=h.currency, split_start=full_start
            )
            begin = full_start
        closes = {d: c for d, c in history.closes.items() if begin <= d <= end}
        splits = history.splits | registry_splits
        _save_splits(session, h.instrument_id, splits, source.name, full_start)
        recorded_splits = {
            day: ratio for day, ratio in recorded_splits.items() if day < full_start
        } | splits
    except (SourceError, UnknownSymbol) as exc:
        outcome.error = str(exc)
        log.warning("%s: %s", h.label, exc)

    _save_prices(session, h.instrument_id, closes, source.name, stale=False)

    existing = dict(
        session.execute(
            select(Price.price_date, Price.close).where(
                Price.instrument_id == h.instrument_id,
                Price.price_date.between(begin, end),
                Price.is_stale.is_(False),
            )
        ).all()
    )
    previous = session.scalar(
        select(Price.close)
        .where(Price.instrument_id == h.instrument_id, Price.price_date < begin)
        .order_by(Price.price_date.desc())
        .limit(1)
    )
    stale = carry_forward(
        h.calendar.trading_days(begin, end),
        existing,
        previous,
        splits={
            day: ratio for day, ratio in (recorded_splits | splits).items() if begin <= day <= end
        },
    )
    valid_stale = {}
    for day, value in stale.items():
        if _fits_numeric(value, 6):
            valid_stale[day] = value
        else:
            error = f"out-of-range carried price for {day}"
            outcome.error = f"{outcome.error}; {error}" if outcome.error else error
            log.warning("%s: %s", h.label, error)
    _save_prices(session, h.instrument_id, valid_stale, source.name, stale=True)

    outcome.fetched = len(closes)
    outcome.carried_forward = len(valid_stale)
    return outcome


def _update_currency(
    session: Session,
    sources: Sequence[FxSource],
    need: CurrencyNeed,
    backfill: bool,
    start: date | None,
    now: datetime,
    force: bool = False,
) -> Outcome:
    outcome = Outcome(need.label, source=sources[0].name if sources else "none")
    keys = (FxRate.currency == need.currency, FxRate.base_currency == need.base_currency)
    last_good = session.scalar(
        select(func.max(FxRate.rate_date)).where(
            *keys, FxRate.rate_date <= need.last_day, FxRate.is_stale.is_(False)
        )
    )
    if (
        not (need.still_held or backfill or start is not None or force)
        and last_good is not None
        and (now.date() - need.last_day).days > REFRESH_DAYS
        and session.scalar(
            select(FxRate.rate_date).where(
                *keys, FxRate.rate_date == calendars.FX.last_trading_day(need.last_day)
            )
        )
        is not None
    ):
        return outcome
    begin = _range(need.first_day, need.last_day, last_good, backfill, start)
    end = need.last_day
    if begin > end:
        return outcome

    expected = set(calendars.FX.trading_days(begin, end))
    if not expected:
        return outcome
    rates_by_source: list[tuple[str, dict[date, Decimal]]] = []
    rates: dict[date, Decimal] = {}
    for source in sources:
        try:
            supplied = source.rates(need.currency, need.base_currency, begin, end)
        except SourceError as exc:
            outcome.fallbacks.append(f"{source.name} failed: {exc}")
            log.warning("%s from %s: %s", need.label, source.name, exc)
            continue
        supplied = {
            day: rate for day, rate in supplied.items() if begin <= day <= end and day not in rates
        }
        if supplied:
            rates.update(supplied)
            rates_by_source.append((source.name, supplied))
        missing = expected - rates.keys()
        if not missing:
            break
        if supplied:
            outcome.fallbacks.append(f"{source.name} left {len(missing)} rate(s) missing")
        else:
            outcome.fallbacks.append(f"{source.name} had no rates")
    if rates_by_source:
        outcome.source = ", ".join(name for name, _rates in rates_by_source)
    else:
        outcome.error = "no source had rates"

    for source_name, source_rates in rates_by_source:
        _save_rates(session, need, source_rates, source_name, stale=False)
    existing = dict(
        session.execute(
            select(FxRate.rate_date, FxRate.rate).where(
                *keys, FxRate.rate_date.between(begin, end), FxRate.is_stale.is_(False)
            )
        ).all()
    )
    previous = session.scalar(
        select(FxRate.rate)
        .where(*keys, FxRate.rate_date < begin)
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    )
    stale = carry_forward(calendars.FX.trading_days(begin, end), existing, previous)
    _save_rates(session, need, stale, outcome.source, stale=True)

    outcome.fetched = len(rates)
    outcome.carried_forward = len(stale)
    return outcome


def _batches(rows: list[dict]) -> Iterable[list[dict]]:
    for i in range(0, len(rows), _BATCH):
        yield rows[i : i + _BATCH]


def _save_prices(
    session: Session, instrument_id: int, closes: Mapping[date, Decimal], source: str, stale: bool
) -> None:
    rows = [
        {
            "instrument_id": instrument_id,
            "price_date": day,
            "close": close,
            "source": source,
            "is_stale": stale,
        }
        for day, close in sorted(closes.items())
    ]
    for batch in _batches(rows):
        stmt = insert(Price).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Price.instrument_id, Price.price_date],
            set_={
                "close": stmt.excluded.close,
                "source": stmt.excluded.source,
                "is_stale": stmt.excluded.is_stale,
                "fetched_at": func.now(),
            },
            # A carried-forward price only ever replaces another carried-forward price.
            where=Price.is_stale if stale else None,
        )
        session.execute(stmt)


def _save_rates(
    session: Session, need: CurrencyNeed, rates: Mapping[date, Decimal], source: str, stale: bool
) -> None:
    rows = [
        {
            "rate_date": day,
            "currency": need.currency,
            "base_currency": need.base_currency,
            "rate": rate,
            "source": source,
            "is_stale": stale,
        }
        for day, rate in sorted(rates.items())
    ]
    for batch in _batches(rows):
        stmt = insert(FxRate).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FxRate.rate_date, FxRate.currency, FxRate.base_currency],
            set_={
                "rate": stmt.excluded.rate,
                "source": stmt.excluded.source,
                "is_stale": stmt.excluded.is_stale,
                "fetched_at": func.now(),
            },
            where=FxRate.is_stale if stale else None,
        )
        session.execute(stmt)


def _save_splits(
    session: Session,
    instrument_id: int,
    splits: Mapping[date, tuple[Decimal, Decimal]],
    source: str,
    split_start: date,
) -> None:
    # A successful full-range scan is authoritative, including an empty result.
    session.execute(
        delete(Split).where(
            Split.instrument_id == instrument_id,
            Split.split_date >= split_start,
            Split.split_date.not_in(splits),
            Split.source != "registry",
        )
    )
    if not splits:
        return
    stmt = insert(Split).values(
        [
            {
                "instrument_id": instrument_id,
                "split_date": day,
                "shares_before": before,
                "shares_after": after,
                "source": source,
            }
            for day, (before, after) in sorted(splits.items())
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Split.instrument_id, Split.split_date],
        set_={
            "shares_before": stmt.excluded.shares_before,
            "shares_after": stmt.excluded.shares_after,
            "source": stmt.excluded.source,
            "fetched_at": func.now(),
        },
        where=Split.source != "registry",
    )
    session.execute(stmt)
