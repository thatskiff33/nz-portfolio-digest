"""Shared Decimal display formatting; calculations retain their full precision."""

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext


def display_number(value: Decimal) -> str:
    with localcontext() as context:
        # Integer digits, two fractional places and a possible rounding carry.
        context.prec = max(context.prec, value.adjusted() + 4)
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not rounded:
            rounded = Decimal("0.00")
        return f"{rounded:,.2f}"


def display_quantity(value: Decimal) -> str:
    """Fixed-point units, trimmed, at most 8 decimal places, half even."""
    return _fixed_point(value, 8, ROUND_HALF_EVEN)


def display_price(value: Decimal) -> str:
    """Fixed-point quoted price, trimmed, at most 4 decimal places, half up."""
    return _fixed_point(value, 4, ROUND_HALF_UP)


def _fixed_point(value: Decimal, places: int, rounding) -> str:
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        # Keep the whole coefficient, and enough digits for a large integer part.
        context.prec = max(
            context.prec,
            len(value.as_tuple().digits) + 1,
            value.adjusted() + places + 2,
        )
        rounded = value.quantize(quantum, rounding=rounding)
        if not rounded:
            return "0"
        rendered = format(rounded, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered
