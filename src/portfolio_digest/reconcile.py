"""Compare stored holding units with a broker or registry snapshot CSV."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from portfolio_digest.importers.base import (
    FileProblem,
    RowError,
    RowProblem,
    listing,
    parse_decimal,
    read_csv,
)
from portfolio_digest.models import Instrument, Trade, TradeType

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
    signed = case((Trade.trade_type == TradeType.SELL.value, -Trade.quantity), else_=Trade.quantity)
    rows = session.execute(
        select(Instrument.exchange, Instrument.code, func.sum(signed))
        .join(Trade, Trade.instrument_id == Instrument.id)
        .where(Trade.portfolio_id == portfolio_id)
        .group_by(Instrument.exchange, Instrument.code)
    )
    return {(exchange, code): units for exchange, code, units in rows}


def compare(
    actual: dict[tuple[str, str], Decimal], snapshot: dict[tuple[str, str], Decimal]
) -> list[Difference]:
    differences = []
    for exchange, code in sorted(actual.keys() | snapshot.keys()):
        app_units = actual.get((exchange, code), Decimal(0))
        snapshot_units = snapshot.get((exchange, code), Decimal(0))
        if app_units != snapshot_units:
            differences.append(Difference(exchange, code, app_units, snapshot_units))
    return differences
