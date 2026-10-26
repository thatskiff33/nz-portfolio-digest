"""Reusable report data and email-safe HTML/plain-text presentation."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from itertools import groupby

from sqlalchemy.orm import Session

from portfolio_digest.returns import ZERO, Result, ReturnError
from portfolio_digest.returns import Report as ReturnsReport
from portfolio_digest.returns_loader import load_combined_returns
from portfolio_digest.returns_table import format_number as money


class ReportError(Exception):
    """A safe report failure without credentials or portfolio data."""


@dataclass(frozen=True)
class ValuePoint:
    day: date
    value: Decimal


@dataclass(frozen=True)
class Report:
    start: date
    end: date
    returns: ReturnsReport
    history: tuple[ValuePoint, ...] = ()

    @property
    def holdings(self) -> tuple[Result, ...]:
        return self.returns.holdings

    @property
    def value(self) -> Decimal:
        return self.returns.total.closing

    @property
    def total_return(self) -> Decimal:
        return self.returns.total.amounts.total

    @property
    def change(self) -> Decimal:
        return self.returns.total.change


def build_report(session: Session, start: date, end: date) -> Report:
    """Inclusive email dates map to the engine's opening-close boundary."""
    if start > end:
        raise ReportError("Report start must be on or before its end.")
    first = start - timedelta(days=1)
    span = (end - first).days
    days = sorted({first + timedelta(days=span * n // 7) for n in range(8)})
    try:
        result = load_combined_returns(session, first, end)
        history = tuple(
            ValuePoint(day, load_combined_returns(session, day, day).total.closing) for day in days
        )
    except ReturnError:
        raise ReportError(
            "Report cannot be valued. Check trade history and run prices fetch --backfill."
        ) from None
    return Report(start, end, result, history)


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
    if any(h.flags for h in report.holdings):
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
    ranked = sorted(report.holdings, key=lambda h: (h.amounts.total, h.exchange, h.label))
    if ranked:
        top, bottom = ranked[-1], ranked[0]
        parts.append(
            '<h2 style="font-size:18px">Movers by dollar return</h2>'
            f"<p>Top: {escape(top.label)} "
            f"<b>NZ$ {money(top.amounts.total)}</b><br>"
            f"Bottom: {escape(bottom.label)} "
            f"<b>NZ$ {money(bottom.amounts.total)}</b></p>"
        )
        text += [
            f"Top mover: {top.label} NZ$ {money(top.amounts.total)}",
            f"Bottom mover: {bottom.label} NZ$ {money(bottom.amounts.total)}",
        ]
    headers = [
        "Holding",
        "Price",
        "Value NZ$",
        "Capital gain",
        "Dividends",
        "Currency gain",
        "Total return",
        "Gross dividends",
        "Withholding tax",
        "Imputation gross-up",
    ]
    core_headers = headers[:7]
    income_headers = [headers[0], *headers[7:]]
    style = 'style="padding:8px 1px;border-bottom:1px solid #dce5df;overflow-wrap:anywhere"'

    def total_row(label: str, result: Result) -> list[str]:
        return [
            label,
            "",
            *(
                money(value)
                for value in (
                    result.closing,
                    result.amounts.capital,
                    result.amounts.dividends,
                    result.amounts.currency,
                    result.amounts.total,
                    result.gross_dividends,
                    result.withholding_tax,
                    result.imputation_gross_up,
                )
            ),
        ]

    def html_row(cells: list[str], bold: bool = False) -> str:
        tag = "th" if bold else "td"
        cell_style = (
            style.replace("padding:", "font-size:9px;padding:")
            if cells in (core_headers, income_headers)
            else style
        )
        return "<tr>" + "".join(f"<{tag} {cell_style}>{escape(c)}</{tag}>" for c in cells) + "</tr>"

    def income_table(rows: tuple[Result, ...], total: Result, label: str) -> str:
        def cells(result: Result, name: str) -> list[str]:
            return [
                name,
                *(
                    money(v)
                    for v in (
                        result.gross_dividends,
                        result.withholding_tax,
                        result.imputation_gross_up,
                    )
                ),
            ]

        return (
            '<p style="font-size:12px;color:#526c62">Income detail (NZ$)</p>'
            '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
            'font-size:10px;text-align:right">'
            + html_row(income_headers, True)
            + "".join(html_row(cells(r, r.label.split(":", 1)[1])) for r in rows)
            + html_row(cells(total, label), True)
            + "</table>"
        )

    for market, group in groupby(report.holdings, key=lambda h: h.exchange):
        rows = tuple(group)
        parts.append(
            f'<h2 style="font-size:18px;margin-top:28px">{escape(market)}</h2>'
            '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
            'font-size:10px;text-align:right">' + html_row(core_headers, True)
        )
        text += ["", market, " | ".join(headers)]
        for row in rows:
            label = row.label.split(":", 1)[1] + (" (stale)" if row.flags else "")
            cells = [
                label,
                f"{money(row.price)} {row.currency}" if row.price is not None else "",
                money(row.closing),
                money(row.amounts.capital),
                money(row.amounts.dividends),
                money(row.amounts.currency),
                money(row.amounts.total),
                money(row.gross_dividends),
                money(row.withholding_tax),
                money(row.imputation_gross_up),
            ]
            parts.append(
                '<tr><td colspan="7" style="padding:10px 1px 0;text-align:left;color:#526c62">'
                f"{escape(row.name)} ({row.units} units)</td></tr>" + html_row(cells[:7])
            )
            text.append(f"{row.name} ({row.units} units)")
            text.append(" | ".join(cells))
        market_result = next(r for r in report.returns.markets if r.label == market)
        subtotal = total_row("Subtotal", market_result)
        parts.append(html_row(subtotal[:7], True) + "</table>")
        parts.append(income_table(rows, market_result, "Subtotal"))
        text.append(" | ".join(subtotal))
    total = total_row("Portfolio total", report.returns.total)
    parts.append(
        '<h2 style="font-size:18px">Portfolio total</h2>'
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
        'font-size:10px;text-align:right">'
        + html_row(core_headers, True)
        + html_row(total[:7], True)
        + "</table>"
    )
    parts.append(income_table((), report.returns.total, "Portfolio total"))
    text += ["", " | ".join(total)]
    parts.append(
        f'<p style="font-size:12px;line-height:1.6;color:#526c62">{NOTE}</p>'
        "</td></tr></table></td></tr></table></body></html>"
    )
    return "\n".join(parts) + "\n", "\n".join(text) + "\n"
