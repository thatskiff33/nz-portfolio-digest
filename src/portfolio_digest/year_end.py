"""NZ tax-year calculations. All amounts are NZD; historical parcels use FIFO."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from portfolio_digest.prices.calendars import FX
from portfolio_digest.returns import (
    ZERO,
    Holding,
    Observation,
    Payment,
    Report,
    ReturnError,
    _financial_math,
    _lookup,
    _matches_split,
    _ordered_trades,
    calculate_returns,
)

NOTES = (
    "Historical FIFO cost, including purchase fees; sale fees reduce proceeds. "
    "The returns engine also uses FIFO, but resets performance parcels to opening market value.",
    "NZ generally does not tax capital gains for long-term investors. Exceptions apply. "
    "This is information, not tax advice.",
    "Gross dividends exclude imputation credits. Withholding is classified as resident for "
    "NZX holdings and foreign otherwise, using the recorded combined withholding field. "
    "Check this classification against your tax statements.",
    "Foreign cost conservatively includes all non-NZX holdings, including Australian shares. "
    "Exemptions may apply. The NZ$50,000 FIF threshold is assessed per person across portfolios; "
    "this report covers this portfolio only and does not calculate FIF income. "
    "Without execution times, same-day additions precede sales for a conservative peak.",
    "DRP income is included; reinvested units must also have a recorded buy or adjustment. "
    "Splits require a matching quantity adjustment. Missing acquisition history uses the "
    "recorded opening-balance or transfer value, which may differ from tax cost.",
    "Total return uses net cash dividends, as in email reports, and the returns engine's "
    "opening value plus purchase outlays denominator. "
    "Carried-forward prices and FX are listed below.",
)


def tax_year(year: int) -> tuple[date, date]:
    if not 2 <= year <= 9999:
        raise ReturnError("Year must be between 2 and 9999 (the year ending 31 March).")
    return date(year - 1, 3, 31), date(year, 3, 31)


@dataclass(frozen=True)
class Dividend:
    holding: str
    day: date
    currency: str
    gross: Decimal
    imputation: Decimal
    withholding: Decimal
    net: Decimal
    fx: Decimal | None = None


@dataclass(frozen=True)
class TaxRow:
    label: str
    gains: Decimal = ZERO
    losses: Decimal = ZERO
    gross: Decimal = ZERO
    imputation: Decimal = ZERO
    resident: Decimal = ZERO
    foreign: Decimal = ZERO
    net: Decimal = ZERO


@dataclass(frozen=True)
class YearEnd:
    year: int
    performance: Report
    rows: tuple[TaxRow, ...]
    total: TaxRow
    foreign_peak: Decimal
    foreign_peak_day: date
    flags: tuple[str, ...]

    @property
    def fif_exceeded(self) -> bool:
        return self.foreign_peak > Decimal(50000)


@_financial_math
def calculate_year_end(
    holdings: tuple[Holding, ...],
    dividends: tuple[Dividend, ...],
    rates: dict[str, tuple[Observation, ...]],
    year: int,
) -> YearEnd:
    start, end = tax_year(year)
    labels = {f"{h.exchange}:{h.code}" for h in holdings}
    if any(p.holding not in labels for p in dividends):
        raise ReturnError("Dividend refers to an unknown holding.")
    holdings = tuple(
        replace(
            h,
            income=tuple(
                Payment(p.day, p.net, p.currency, p.fx, p.gross, p.withholding, p.imputation)
                for p in dividends
                if p.holding == f"{h.exchange}:{h.code}"
            ),
        )
        for h in holdings
    )
    performance = calculate_returns(holdings, start, end, rates, "NZD")
    flags = set(performance.total.flags)

    def fx(currency: str, day: date, explicit: Decimal | None) -> Decimal:
        if currency == "NZD":
            if explicit is not None and explicit != Decimal(1):
                raise ReturnError("Base-currency FX must be one.")
            return Decimal(1)
        rate = (
            explicit
            if explicit is not None
            else _lookup(rates.get(currency, ()), day, f"{currency}/NZD FX", flags, FX)
        )
        if not rate.is_finite() or rate <= ZERO:
            raise ReturnError("FX must be positive and finite.")
        return rate

    # Each parcel retains historical NZD cost, including acquisition fees.
    parcels: dict[str, list[list[Decimal]]] = {}
    quantities: dict[str, Decimal] = {}
    stats = {f"{h.exchange}:{h.code}": [ZERO] * 7 for h in holdings}
    events = []
    foreign = set()
    for h in holdings:
        label = f"{h.exchange}:{h.code}"
        parcels[label] = []
        quantities[label] = ZERO
        if h.exchange != "NZX":
            foreign.add(label)
        splits = {s.day: s for s in h.splits}
        held = ZERO
        previous_day = None
        ordered = _ordered_trades(h, end)
        # A completed position before the opening close contributes no tax cost.
        # Drop its history, including FX requirements, even if this holding is rebought.
        replay_from = 0
        historical_quantity = ZERO
        for index, trade in enumerate(ordered):
            if trade.day > start:
                break
            historical_quantity += trade.delta
            if historical_quantity == ZERO:
                replay_from = index + 1
        for index, trade in enumerate(ordered[replay_from:]):
            split = splits.get(trade.day)
            is_split = (
                trade.day != previous_day
                and trade.kind == "adjustment"
                and split
                and _matches_split(held, trade.quantity, split)
            )
            priority = 0 if is_split else (2 if trade.delta < ZERO else 1)
            events.append((trade.day, priority, label, index, h, trade, splits))
            held += trade.delta
            previous_day = trade.day
    peak = ZERO
    peak_day = start
    seen: set[tuple[str, date]] = set()
    for day, _, label, _, h, trade, splits in sorted(events, key=lambda e: e[:4]):
        held = quantities[label]
        lots = parcels[label]
        split = splits.get(day)
        first = (label, day) not in seen
        seen.add((label, day))
        if (
            trade.kind == "adjustment"
            and split
            and first
            and _matches_split(held, trade.quantity, split)
        ):
            new = held + trade.quantity
            for lot in lots[:-1]:
                lot[0] = lot[0] * new / held
            lots[-1][0] = new - sum((p[0] for p in lots[:-1]), ZERO)
            # Rare split fees are acquisition costs, allocated across surviving lots.
            if trade.fees:
                fee = trade.fees * fx(h.currency, day, trade.fx)
                remaining = fee
                for lot in lots[:-1]:
                    share = fee * lot[0] / new
                    lot[1] += share
                    remaining -= share
                lots[-1][1] += remaining
        else:
            rate = fx(h.currency, day, trade.fx)
            if trade.delta > ZERO:
                lots.append([trade.quantity, (trade.quantity * trade.price + trade.fees) * rate])
            else:
                units = -trade.delta
                if units > held:
                    raise ReturnError(f"{h.code}: sale exceeds held quantity.")
                cost = ZERO
                while units:
                    lot = lots[0]
                    taken = min(units, lot[0])
                    part = lot[1] if taken == lot[0] else lot[1] * taken / lot[0]
                    cost += part
                    lot[0] -= taken
                    lot[1] -= part
                    units -= taken
                    if not lot[0]:
                        lots.pop(0)
                if trade.kind == "sell" and start < day <= end:
                    gain = (trade.quantity * trade.price - trade.fees) * rate - cost
                    stats[label][0 if gain >= ZERO else 1] += gain
        quantities[label] = held + trade.delta
        current = sum((lot[1] for key in foreign for lot in parcels[key]), ZERO)
        if day <= start:
            peak, peak_day = current, start
        elif current > peak:
            peak, peak_day = current, day
    for payment in dividends:
        if not start < payment.day <= end:
            continue
        values = (payment.gross, payment.imputation, payment.withholding, payment.net)
        if any(not isinstance(v, Decimal) or not v.is_finite() or v < ZERO for v in values):
            raise ReturnError("Dividend amounts must be finite non-negative Decimals.")
        row = stats.setdefault(payment.holding, [ZERO] * 7)
        rate = fx(payment.currency, payment.day, payment.fx)
        row[2] += payment.gross * rate
        row[3] += payment.imputation * rate
        row[5 if payment.holding in foreign else 4] += payment.withholding * rate
        row[6] += payment.net * rate
    active = {r.label for r in performance.holdings}
    rows = tuple(TaxRow(k, *v) for k, v in sorted(stats.items()) if k in active or any(v))
    total = TaxRow(
        "Portfolio",
        *(
            sum((getattr(r, f) for r in rows), ZERO)
            for f in ("gains", "losses", "gross", "imputation", "resident", "foreign", "net")
        ),
    )
    return YearEnd(year, performance, rows, total, peak, peak_day, tuple(sorted(flags)))
