"""Yahoo is a missing-income prompt, never an accounting source of truth."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importers.base import RowProblem
from portfolio_digest.importing import ImportFailure
from portfolio_digest.income import INCOME_FIELDS, ParsedIncome, store_income
from portfolio_digest.models import DividendSuggestion, Income, Instrument, Portfolio
from portfolio_digest.returns import instrument_position


def suggest(
    session: Session, portfolio: Portfolio, instrument: Instrument, events: dict[date, Decimal]
) -> list[DividendSuggestion]:
    session.get(Portfolio, portfolio.id, with_for_update=True)
    suggestions = []
    for day, per_unit in sorted(events.items()):
        if not isinstance(per_unit, Decimal) or not per_unit.is_finite() or per_unit <= 0:
            raise RowProblem("Dividend per unit must be a positive finite Decimal.")
        # Ex-date holdings exclude purchases on that day; splits apply before entitlement.
        units = instrument_position(
            session, portfolio.id, instrument.id, day, before_trades=True
        ).units
        if units <= 0:
            continue
        existing = session.scalar(
            select(DividendSuggestion).where(
                DividendSuggestion.portfolio_id == portfolio.id,
                DividendSuggestion.instrument_id == instrument.id,
                DividendSuggestion.ex_date == day,
            )
        )
        if existing:
            suggestions.append(existing)
            continue
        confirmed = session.scalar(
            select(Income.id).where(
                Income.portfolio_id == portfolio.id,
                Income.instrument_id == instrument.id,
                Income.ex_date == day,
            )
        )
        if confirmed:
            continue
        amount = (units * per_unit).quantize(Decimal("0.000001"))
        if amount >= Decimal("1e14"):
            raise RowProblem("Suggested dividend is outside the supported money range.")
        suggestion = DividendSuggestion(
            portfolio_id=portfolio.id, instrument_id=instrument.id, ex_date=day, gross_amount=amount
        )
        session.add(suggestion)
        session.flush()
        suggestions.append(suggestion)
    return suggestions


def decide(
    session: Session,
    suggestion_id: int,
    *,
    reject: bool = False,
    confirmed: ParsedIncome | None = None,
    evidence: str | None = None,
) -> DividendSuggestion:
    item = session.get(DividendSuggestion, suggestion_id)
    if item is None:
        raise ImportFailure("No dividend suggestion with that ID.")
    session.get(Portfolio, item.portfolio_id, with_for_update=True)
    session.refresh(item, with_for_update=True)
    if item.status != "pending":
        raise ImportFailure(f"Suggestion is already {item.status}.")
    if reject:
        item.status = "rejected"
        return item
    if confirmed is None or not evidence or evidence not in ("broker", "registry"):
        raise RowProblem(
            "Confirmation requires broker or registry evidence and actual payment amounts."
        )
    instrument = session.get(Instrument, item.instrument_id)
    if (confirmed.exchange, confirmed.code, confirmed.currency, confirmed.ex_date) != (
        instrument.exchange,
        instrument.code,
        instrument.currency,
        item.ex_date,
    ):
        raise RowProblem("Confirmation must match the suggested listing, currency and ex date.")
    candidates = list(
        session.scalars(
            select(Income).where(
                Income.portfolio_id == item.portfolio_id,
                Income.instrument_id == item.instrument_id,
                Income.paid_date == confirmed.paid_date,
                (Income.ex_date == item.ex_date) | Income.ex_date.is_(None),
            )
        )
    )
    if candidates:
        matching = [
            income
            for income in candidates
            if all(
                getattr(income, field) == getattr(confirmed, field)
                for field in INCOME_FIELDS
                if field != "ex_date"
            )
        ]
        if len(matching) != 1:
            raise RowProblem("Existing broker income differs or is ambiguous; reconcile it first.")
        income = matching[0]
    else:
        income, _ = store_income(
            session, item.portfolio_id, item.instrument_id, confirmed, evidence
        )
    item.status, item.income_id = "confirmed", income.id
    return item
