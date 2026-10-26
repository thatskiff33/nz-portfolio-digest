"""Plain text Sharesight-style columns; round only for display."""

from decimal import ROUND_HALF_UP, Decimal, localcontext

from portfolio_digest.returns import Report, Result
from portfolio_digest.terminal import terminal_text


def format_number(value: Decimal) -> str:
    with localcontext() as context:
        # Integer digits, two fractional places and a possible rounding carry.
        context.prec = max(context.prec, value.adjusted() + 4)
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def render_table(report: Report) -> str:
    unit = "NZ$" if report.base_currency == "NZD" else report.base_currency
    headers = [
        "Holding / market",
        f"Value ({unit})",
        "Capital gain",
        "Dividends",
        "Currency gain",
        "Total return",
        "Gross dividends",
        "Withholding tax",
        "Imputation gross-up",
    ]

    def row(result: Result) -> list[str]:
        percentages = result.percentages
        values = [
            terminal_text(result.label) + (" *" if result.flags else ""),
            format_number(result.closing),
        ]
        for field in ("capital", "dividends", "currency", "total"):
            percent = f"{format_number(getattr(percentages, field))}%" if percentages else "n/a"
            values.append(f"{format_number(getattr(result.amounts, field))} ({percent})")
        values.extend(
            format_number(v)
            for v in (result.gross_dividends, result.withholding_tax, result.imputation_gross_up)
        )
        return values

    rows = [headers]
    for market in report.markets:
        rows.extend(row(h) for h in report.holdings if h.exchange == market.label)
        rows.append(row(market))
    rows.append(row(report.total))
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    lines = [f"Returns {report.start} to {report.end} ({unit})"]
    lines.extend(" | ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in rows)
    if report.total.flags:
        lines.append("* Stale or carried-forward observations:")
        lines.extend(f"  {terminal_text(flag)}" for flag in report.total.flags)
    return "\n".join(lines)
