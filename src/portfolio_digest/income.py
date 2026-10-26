"""Validated broker income and atomic persistence, including linked DRP purchases."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importers.base import (
    RowProblem,
    checked_text,
    parse_date,
    parse_money,
    parse_price,
    parse_quantity,
)
from portfolio_digest.models import Income, Trade

ZERO = Decimal(0)


@dataclass(frozen=True)
class ParsedIncome:
    where: str
    exchange: str
    code: str
    paid_date: date
    currency: str
    gross_amount: Decimal
    net_amount: Decimal
    withholding_tax: Decimal
    external_ref: str
    kind: str = "dividend"
    imputation_credits: Decimal = ZERO
    resident_withholding_tax: Decimal = ZERO
    foreign_withholding_tax: Decimal = ZERO
    franking_credits: Decimal = ZERO
    drp_quantity: Decimal | None = None
    ex_date: date | None = None
    fx_rate: Decimal | None = None


def parse_income(where, row, exchange, code, currency, reference) -> ParsedIncome:
    def money(key):
        return parse_money(row.get(key, ""), key, currency=currency, allow_negative=False)

    checked_text(reference, "Income reference")
    if not reference.strip() or len(reference) > 110:
        raise RowProblem("Income reference must contain 1 to 110 characters.")
    if row.get("kind", "dividend") not in ("dividend", "distribution"):
        raise RowProblem("Income kind must be dividend or distribution.")
    if not row.get("gross") or not row.get("net"):
        raise RowProblem("Dividend requires explicit gross and net amounts; tax is never guessed.")
    gross, net = money("gross"), money("net")
    rwt, foreign, tax = money("rwt"), money("foreign_tax"), money("tax")
    if tax and (rwt or foreign) and tax != rwt + foreign:
        raise RowProblem("Total withholding differs from RWT plus foreign withholding.")
    tax = tax or rwt + foreign
    if gross != net + tax:
        raise RowProblem("Gross dividend must equal net dividend plus withholding tax.")
    return ParsedIncome(
        where=where,
        exchange=exchange,
        code=code,
        currency=currency,
        paid_date=parse_date(row["date"]),
        gross_amount=gross,
        net_amount=net,
        withholding_tax=tax,
        external_ref=reference,
        kind=row.get("kind") or "dividend",
        imputation_credits=money("imputation"),
        resident_withholding_tax=rwt,
        foreign_withholding_tax=foreign,
        franking_credits=money("franking"),
        drp_quantity=parse_quantity(row["drp"]) if row.get("drp") else None,
        ex_date=parse_date(row["ex_date"]) if row.get("ex_date") else None,
    )


INCOME_FIELDS = (
    "paid_date",
    "currency",
    "gross_amount",
    "net_amount",
    "withholding_tax",
    "kind",
    "imputation_credits",
    "resident_withholding_tax",
    "foreign_withholding_tax",
    "franking_credits",
    "drp_quantity",
    "ex_date",
    "fx_rate",
)


def store_income(
    session: Session,
    portfolio_id: int,
    instrument_id: int,
    parsed: ParsedIncome,
    source: str,
    run_id: int | None = None,
) -> tuple[Income, bool]:
    existing = session.scalar(
        select(Income).where(
            Income.portfolio_id == portfolio_id,
            Income.source == source,
            Income.external_ref == parsed.external_ref,
        )
    )
    values = {key: getattr(parsed, key) for key in INCOME_FIELDS}
    if existing:
        if existing.instrument_id != instrument_id or any(
            getattr(existing, key) != value for key, value in values.items()
        ):
            raise RowProblem("Income reference already exists with different fields.")
        return existing, False
    drp_price = None
    if parsed.drp_quantity:
        if parsed.net_amount == 0:
            raise RowProblem("DRP units require a positive reinvested net amount.")
        drp_price = parse_price(str(parsed.net_amount / parsed.drp_quantity), "DRP unit price")
    income = Income(
        portfolio_id=portfolio_id,
        instrument_id=instrument_id,
        source=source,
        external_ref=parsed.external_ref,
        import_run_id=run_id,
        **values,
    )
    session.add(income)
    session.flush()
    if parsed.drp_quantity:
        # Net is the exact cost. Unit price can round, so retain the exact consideration.
        session.add(
            Trade(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                trade_date=parsed.paid_date,
                trade_type="buy",
                quantity=parsed.drp_quantity,
                price=drp_price,
                consideration=parsed.net_amount,
                currency=parsed.currency,
                fx_rate=parsed.fx_rate,
                source=source,
                external_ref="drp:" + parsed.external_ref,
                import_run_id=run_id,
                income_id=income.id,
            )
        )
    return income, True
