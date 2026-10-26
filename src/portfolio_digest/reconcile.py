"""Provider-independent unit reconciliation. Confirmation never edits the ledger."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.models import Income, Trade, TradeType


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
        Mismatch(key, ledger.get(key, Decimal(0)), observed.get(key, Decimal(0)))
        for key in sorted(ledger.keys() | observed.keys())
        if ledger.get(key, Decimal(0)) != observed.get(key, Decimal(0))
    ]
