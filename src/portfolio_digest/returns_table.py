"""Plain text Sharesight-style columns; round only for display."""

from decimal import ROUND_HALF_UP, Decimal

from portfolio_digest.returns import Report, Result


def _number(value: Decimal) -> str:
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
    ]

    def row(result: Result) -> list[str]:
        percentages = result.percentages
        values = [result.label + (" *" if result.flags else ""), _number(result.closing)]
        for field in ("capital", "dividends", "currency", "total"):
            percent = f"{_number(getattr(percentages, field))}%" if percentages else "n/a"
            values.append(f"{_number(getattr(result.amounts, field))} ({percent})")
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
        lines.extend(f"  {flag}" for flag in report.total.flags)
    return "\n".join(lines)
