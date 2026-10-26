"""Reusable report data and email-safe HTML/plain-text presentation."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from itertools import groupby

from portfolio_digest.returns import ZERO, HoldingReturn, PeriodEngine


@dataclass(frozen=True)
class ValuePoint:
    day: date
    value: Decimal


@dataclass(frozen=True)
class Report:
    start: date
    end: date
    holdings: tuple[HoldingReturn, ...]
    history: tuple[ValuePoint, ...] = ()

    @property
    def value(self) -> Decimal:
        return sum((h.value for h in self.holdings), ZERO)

    @property
    def total_return(self) -> Decimal:
        return sum((h.total_return for h in self.holdings), ZERO)

    @property
    def change(self) -> Decimal:
        return self.value - sum((h.opening_value for h in self.holdings), ZERO)


def build_report(engine: PeriodEngine, start: date, end: date) -> Report:
    holdings = engine.calculate(start, end)
    # At most eight real valuations, including opening and closing values.
    first = start - timedelta(days=1)
    span = (end - first).days
    days = sorted({first + timedelta(days=span * n // 7) for n in range(8)})
    history = tuple(
        ValuePoint(day, sum((h.value for h in engine.calculate(day, day)), ZERO)) for day in days
    )
    return Report(start, end, holdings, history)


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


NOTE = (
    "All gains and values are NZ dollars. Price is in the holding's currency. "
    "Change in value includes purchases and sales; total return excludes those flows. "
    "Dividends are net cash received. Holdings are combined across portfolios by listing. "
    "Sold holdings with period activity are included. Stale flags cover prices and FX."
)


def render(report: Report) -> tuple[str, str]:
    heading = f"Portfolio digest | {report.start:%d %b %Y} to {report.end:%d %b %Y}"
    text = [
        heading,
        f"Portfolio value NZ$ {money(report.value)}",
        f"Change in value NZ$ {money(report.change)}",
        f"Total return NZ$ {money(report.total_return)}",
        "",
        NOTE,
        "",
    ]
    parts = [
        '<!doctype html><html lang="en-NZ"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;background:#eef3f1;color:#17342c;font-family:Arial,sans-serif">'
        '<table role="presentation" style="width:100%;border-collapse:collapse"><tr><td '
        'style="padding:12px"><table role="presentation" align="center" '
        'style="width:100%;max-width:760px;border-collapse:collapse;background:white"><tr>'
        '<td style="padding:20px 12px">'
        '<p style="font-size:12px;color:#526c62;letter-spacing:2px">PORTFOLIO DIGEST</p>',
        f'<h1 style="font-size:28px;margin:8px 0">NZ$ {money(report.value)}</h1>',
        f'<p style="font-size:13px">{report.start:%d %b %Y} to {report.end:%d %b %Y}</p>',
        f"<p>Change in value <b>NZ$ {money(report.change)}</b><br>"
        f"Total return <b>NZ$ {money(report.total_return)}</b></p>",
    ]
    if not report.holdings:
        parts.append("<p>No holdings or activity in this period.</p>")
        text.append("No holdings or activity in this period.")
    if any(h.stale for h in report.holdings):
        parts.append(
            '<p style="padding:10px;background:#fff0cb">'
            "Stale prices or FX: flagged values may be out of date.</p>"
        )
        text.append("Stale prices or FX: flagged values may be out of date.")
    if report.history:
        parts.append(
            '<h2 style="font-size:18px">Value through the period</h2>'
            '<table aria-label="Portfolio value history" '
            'style="width:100%;font-size:12px;table-layout:fixed;border-collapse:collapse">'
        )
        maximum = max((p.value for p in report.history), default=ZERO) or Decimal(1)
        for point in report.history:
            width = max(1, int(point.value / maximum * 100))
            parts.append(
                f'<tr><td style="width:58px">{point.day:%d %b}</td><td>'
                f'<table role="presentation" width="{width}%" '
                'style="border-collapse:collapse"><tr><td height="12" '
                'style="background:#28765b;font-size:1px">&nbsp;</td></tr></table>'
                f'</td><td style="width:90px;text-align:right">{money(point.value)}</td></tr>'
            )
            text.append(f"{point.day.isoformat()}: NZ$ {money(point.value)}")
        parts.append("</table>")
    ranked = sorted(report.holdings, key=lambda h: (h.total_return, h.market, h.code))
    if ranked:
        top, bottom = ranked[-1], ranked[0]
        parts.append(
            '<h2 style="font-size:18px">Movers by dollar return</h2>'
            f"<p>Top: {escape(top.market)}:{escape(top.code)} "
            f"<b>NZ$ {money(top.total_return)}</b><br>"
            f"Bottom: {escape(bottom.market)}:{escape(bottom.code)} "
            f"<b>NZ$ {money(bottom.total_return)}</b></p>"
        )
        text += [
            f"Top mover: {top.market}:{top.code} NZ$ {money(top.total_return)}",
            f"Bottom mover: {bottom.market}:{bottom.code} NZ$ {money(bottom.total_return)}",
        ]
    headers = [
        "Holding",
        "Price",
        "Value NZ$",
        "Capital gain",
        "Dividends",
        "Currency gain",
        "Total return",
    ]
    style = 'style="padding:8px 1px;border-bottom:1px solid #dce5df;overflow-wrap:anywhere"'

    def total_row(label: str, rows: tuple[HoldingReturn, ...]) -> list[str]:
        return [
            label,
            "",
            *(
                money(sum((getattr(r, key) for r in rows), ZERO))
                for key in ("value", "capital_gain", "dividends", "currency_gain", "total_return")
            ),
        ]

    def html_row(cells: list[str], bold: bool = False) -> str:
        tag = "th" if bold else "td"
        cell_style = (
            style.replace("padding:", "font-size:9px;padding:") if cells == headers else style
        )
        return "<tr>" + "".join(f"<{tag} {cell_style}>{escape(c)}</{tag}>" for c in cells) + "</tr>"

    for market, group in groupby(report.holdings, key=lambda h: h.market):
        rows = tuple(group)
        parts.append(
            f'<h2 style="font-size:18px;margin-top:28px">{escape(market)}</h2>'
            '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
            'font-size:10px;text-align:right">' + html_row(headers, True)
        )
        text += ["", market, " | ".join(headers)]
        for row in rows:
            label = row.code + (" (stale)" if row.stale else "")
            cells = [
                label,
                f"{money(row.price)} {row.currency}",
                money(row.value),
                money(row.capital_gain),
                money(row.dividends),
                money(row.currency_gain),
                money(row.total_return),
            ]
            parts.append(
                '<tr><td colspan="7" style="padding:10px 1px 0;text-align:left;color:#526c62">'
                f"{escape(row.name)} ({row.units} units)</td></tr>" + html_row(cells)
            )
            text.append(f"{row.name} ({row.units} units)")
            text.append(" | ".join(cells))
        subtotal = total_row("Subtotal", rows)
        parts.append(html_row(subtotal, True) + "</table>")
        text.append(" | ".join(subtotal))
    total = total_row("Portfolio total", report.holdings)
    parts.append(
        '<h2 style="font-size:18px">Portfolio total</h2>'
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
        'font-size:10px;text-align:right">'
        + html_row(headers, True)
        + html_row(total, True)
        + "</table>"
    )
    text += ["", " | ".join(total)]
    parts.append(
        f'<p style="font-size:12px;line-height:1.6;color:#526c62">{NOTE}</p>'
        "</td></tr></table></td></tr></table></body></html>"
    )
    return "\n".join(parts) + "\n", "\n".join(text) + "\n"
