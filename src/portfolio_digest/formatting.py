"""Shared Decimal display formatting; calculations retain their full precision."""

from decimal import ROUND_HALF_UP, Decimal, localcontext


def display_number(value: Decimal) -> str:
    with localcontext() as context:
        # Integer digits, two fractional places and a possible rounding carry.
        context.prec = max(context.prec, value.adjusted() + 4)
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
