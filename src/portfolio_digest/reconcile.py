"""Provider-independent unit reconciliation. Confirmation never edits the ledger."""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importers.base import (
    FileProblem,
    RowError,
    RowProblem,
    listing,
    parse_decimal,
    read_csv,
)
from portfolio_digest.models import Income, Instrument, Trade, TradeType


@dataclass(frozen=True)
class Mismatch:
    instrument_id: int
    ledger: Decimal
    observed: Decimal

    @property
    def difference(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return self.observed - self.ledger


def ledger_units(session: Session, portfolio_id: int) -> dict[int, Decimal]:
    """Include opening balances, signed adjustments and explicit DRP units."""
    totals: dict[int, Decimal] = defaultdict(Decimal)
    with localcontext() as context:
        context.prec = 50
        for trade in session.scalars(select(Trade).where(Trade.portfolio_id == portfolio_id)):
            sign = -1 if trade.trade_type == TradeType.SELL else 1
            totals[trade.instrument_id] += sign * trade.quantity
        for income in session.scalars(select(Income).where(Income.portfolio_id == portfolio_id)):
            if income.drp_quantity is not None:
                totals[income.instrument_id] += income.drp_quantity
    return dict(totals)


def reconcile(ledger: dict[int, Decimal], observed: dict[int, Decimal]) -> list[Mismatch]:
    """Compare the union so missing holdings on either side cannot disappear."""
    return [
        Mismatch(key, ledger_units, observed_units)
        for key, ledger_units, observed_units in _unit_differences(ledger, observed)
    ]


Key = TypeVar("Key", int, tuple[str, str])


def _unit_differences(
    actual: dict[Key, Decimal], snapshot: dict[Key, Decimal]
) -> Iterator[tuple[Key, Decimal, Decimal]]:
    for key in sorted(actual.keys() | snapshot.keys()):
        app_units = actual.get(key, Decimal(0))
        snapshot_units = snapshot.get(key, Decimal(0))
        if app_units != snapshot_units:
            yield key, app_units, snapshot_units


COLUMNS = {
    "exchange": ("Exchange", "Market", "Market Code"),
    "code": ("Code", "Instrument Code", "Symbol", "Ticker"),
    "units": ("Units", "Quantity", "Balance"),
}


@dataclass(frozen=True)
class Difference:
    exchange: str
    code: str
    app_units: Decimal
    snapshot_units: Decimal

    @property
    def variance(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return self.app_units - self.snapshot_units


def parse_snapshot(content: bytes) -> dict[tuple[str, str], Decimal]:
    holdings: dict[tuple[str, str], Decimal] = {}
    errors: list[RowError] = []
    for line, row in read_csv(content, COLUMNS, set(COLUMNS)):
        try:
            exchange, code, _ = listing(row["exchange"], row["code"])
            units = parse_decimal(row["units"], "Units")
            if units < 0:
                raise RowProblem(f"Units cannot be negative ({row['units']!r}).")
            key = (exchange, code)
            if key in holdings:
                raise RowProblem(f"{exchange}:{code} appears more than once.")
            holdings[key] = units
        except RowProblem as exc:
            errors.append(RowError(f"line {line}", str(exc)))
    if errors:
        raise FileProblem("Snapshot has errors:\n" + "\n".join(str(error) for error in errors))
    return holdings


def stored_units(session: Session, portfolio_id: int) -> dict[tuple[str, str], Decimal]:
    units = ledger_units(session, portfolio_id)
    if not units:
        return {}
    instruments = session.scalars(select(Instrument).where(Instrument.id.in_(units)))
    return {(item.exchange, item.code): units[item.id] for item in instruments}


def compare(
    actual: dict[tuple[str, str], Decimal], snapshot: dict[tuple[str, str], Decimal]
) -> list[Difference]:
    return [
        Difference(exchange, code, app_units, snapshot_units)
        for (exchange, code), app_units, snapshot_units in _unit_differences(actual, snapshot)
    ]
