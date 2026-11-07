"""Shared ledger unit scale and split price arithmetic, without caller context changes."""

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from portfolio_digest.models import FINANCIAL_PRECISION, QUANTITY_SCALE

UNIT_SCALE = Decimal(1).scaleb(-QUANTITY_SCALE)


def round_units(units: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, FINANCIAL_PRECISION)
        return units.quantize(UNIT_SCALE, rounding=ROUND_HALF_EVEN)


def split_target(held: Decimal, before: Decimal, after: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, FINANCIAL_PRECISION)
        return round_units(held * after / before)


def split_change(held: Decimal, before: Decimal, after: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, FINANCIAL_PRECISION)
        return split_target(held, before, after) - held


def rescale_price(price: Decimal, before: Decimal, after: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, FINANCIAL_PRECISION)
        return price * before / after


def split_warning(day: date) -> str:
    return f"Split on {day} not yet recorded; add the adjustment"
