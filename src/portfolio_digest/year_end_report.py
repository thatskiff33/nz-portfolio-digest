"""Email-safe HTML and CSV views of the same tax-year figures."""

import csv
import io
from html import escape

from portfolio_digest.formatting import display_number
from portfolio_digest.returns import _financial_math
from portfolio_digest.year_end import NOTES, YearEnd

COLUMNS = (
    ("gains", "Realised gains"),
    ("losses", "Realised losses"),
    ("gross", "Gross dividends"),
    ("imputation", "Imputation credits"),
    ("resident", "Resident withholding"),
    ("foreign", "Foreign withholding"),
    ("net", "Net received / reinvested"),
)


def safe_cell(value: str) -> str:
    """Keep untrusted labels from becoming spreadsheet formulae."""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


@_financial_math
def render_csv(report: YearEnd) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Year ending 31 March", report.year, "Currency", "NZD"])
    writer.writerow(["Holding", *(title for _, title in COLUMNS)])
    for row in (*report.rows, report.total):
        writer.writerow([safe_cell(row.label), *(getattr(row, f) for f, _ in COLUMNS)])
    writer.writerow([])
    writer.writerow(["Holding", "Opening value", "Closing value", "Total return", "Return %"])
    for row in (*report.performance.holdings, report.performance.total):
        pct = row.percentages
        writer.writerow(
            [
                safe_cell(row.label),
                row.opening,
                row.closing,
                row.amounts.total,
                pct.total if pct else "N/A",
            ]
        )
    writer.writerow(
        ["Peak foreign historical cost", report.foreign_peak, report.foreign_peak_day.isoformat()]
    )
    writer.writerow(["Over NZ$50,000 FIF threshold", "Yes" if report.fif_exceeded else "No"])
    for note in (*NOTES, *report.flags):
        writer.writerow(["Note", safe_cell(note)])
    return output.getvalue()


@_financial_math
def render_html(report: YearEnd, name: str) -> str:
    def table(headers, rows):
        head = "".join(
            f'<th scope="col" style="padding:8px;text-align:left">{escape(h)}</th>' for h in headers
        )
        body = "".join(
            "<tr>"
            + "".join(
                f'<td data-label="{escape(headers[i], quote=True)}" '
                f'style="padding:8px;border-top:1px solid #d9e3e5">{escape(str(v))}</td>'
                for i, v in enumerate(row)
            )
            + "</tr>"
            for row in rows
        )
        return (
            f'<div style="overflow-x:auto"><table style="border-collapse:collapse;'
            f'width:100%;font-size:14px"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )

    tax = table(
        ["Holding", *(title for _, title in COLUMNS)],
        [
            [row.label, *(display_number(getattr(row, f)) for f, _ in COLUMNS)]
            for row in (*report.rows, report.total)
        ],
    )
    performance = table(
        ["Holding", "Opening", "Closing", "Total return", "Return %"],
        [
            [
                r.label,
                display_number(r.opening),
                display_number(r.closing),
                display_number(r.amounts.total),
                f"{display_number(r.percentages.total)}%" if r.percentages else "N/A",
            ]
            for r in (*report.performance.holdings, report.performance.total)
        ],
    )
    notes = "".join(f"<li>{escape(n)}</li>" for n in NOTES)
    flags = "".join(f"<li>{escape(n)}</li>" for n in report.flags)
    warning = (
        "Over the NZ$50,000 FIF threshold. Check whether FIF rules apply."
        if report.fif_exceeded
        else "This portfolio did not exceed NZ$50,000."
    )
    return f"""<!doctype html>
<html lang="en-NZ"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Year-end summary {report.year}</title>
<style>
@media (max-width:640px) {{
  table, tbody, tr, td {{ display:block; }}
  thead {{ display:none; }}
  tr {{ margin-bottom:16px; border:1px solid #d9e3e5; }}
  td {{ display:flex; justify-content:space-between; gap:16px; text-align:right; }}
  td::before {{ content:attr(data-label); text-align:left; font-weight:bold; }}
  td:first-child {{ background:#eef3f4; font-weight:bold; }}
}}
</style></head>
<body style="margin:0;background:#eef3f4;color:#19383d;font-family:Arial,sans-serif">
<main style="max-width:1000px;margin:24px auto;padding:24px;background:white">
<p style="color:#327568">PORTFOLIO DIGEST · NZ TAX YEAR</p>
<h1>{escape(name)}: year-end summary</h1>
<p>1 April {report.year - 1} to 31 March {report.year}. All amounts in NZ$.</p>
<h2>Realised sales and income</h2>{tax}
<h2>Portfolio performance</h2>
<p>Opening: 31 March {report.year - 1}. Closing: 31 March {report.year}.</p>{performance}
<h2>Foreign shares cost</h2>
<p>Peak historical cost: <strong>NZ${display_number(report.foreign_peak)}</strong>
 on {report.foreign_peak_day}. {warning}</p>
<h2>How to read this summary</h2><ul>{notes}</ul>
<h2>Data quality</h2><ul>{flags or "<li>No carried-forward data flags.</li>"}</ul>
<p>The attached CSV contains the same figures at full calculation precision.</p>
</main></body></html>"""
