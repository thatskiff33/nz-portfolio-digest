# nz-portfolio-digest

A self-hosted share portfolio tracker for New Zealand investors. It imports trades
straight from your brokers and share registries, values **every** holding in NZ$ each day,
and sends you a performance email (plus a private dashboard) on the schedule you choose.

Built to replace the Sharesight free plan, which only shows 10 holdings.

## Status

Under construction. The plan and progress are tracked in the Epic issue (#14).

| Wave | What it delivers |
|---|---|
| 1. Foundation | Database, server deploy, importing every holding, daily prices and exchange rates |
| 2. Sharesight parity | Returns (capital, dividend and currency gain), scheduled email, dashboard |
| 3. Beyond Sharesight | Dividends and imputation credits, live connections (Akahu), extras |

## Running it

See [docs/DEPLOY.md](docs/DEPLOY.md).

```sh
portfolio-digest migrate          # bring the database up to date
portfolio-digest health           # check the database is reachable and migrated
portfolio-digest portfolio add "Personal"            # --base-currency AUD for others
portfolio-digest portfolio list
portfolio-digest prices fetch --backfill              # initial price and FX history
portfolio-digest prices fetch                         # normal scheduled/incremental run
portfolio-digest prices check                         # missing, stale, zero or suspect data

# Import a complete trade history, then prove its units against a current snapshot.
portfolio-digest import sharesies transactions.csv --portfolio "Personal"
portfolio-digest import asb-statement statement.txt --portfolio "Personal"
portfolio-digest import sharesight-csv trades.csv --portfolio "Personal"
portfolio-digest reconcile sharesies holdings.csv --portfolio "Personal"

portfolio-digest import list
portfolio-digest import undo 12
portfolio-digest returns --portfolio "Personal" --period week
portfolio-digest returns --portfolio "Personal" --period 2026-10-01:2026-10-26
```

NZX and ASX codes map automatically to Yahoo's `.NZ` and `.AX` symbols. NASDAQ and NYSE
codes are used as-is. Set an exception for a right, renamed or delisted security with
`portfolio-digest prices symbol NZX:AIRRG AIRRG.NZ`. See the deployment guide for the
twice-daily schedule and recovery commands. Stored closes are raw, unadjusted prices in
major currency units. ADJUSTMENT trades record quantity changes. The returns engine
rescales parcels when an adjustment matches a stored Split row. Other adjustments
move units at their recorded price without cash, per issue #19. Split rows alone
never change quantities; do not split-adjust these closes again.

The app also reads ASB Securities contract notes from one Gmail label with read-only
access, and supports manual corrections, gifted shares and opening balances. See
[docs/IMPORTING.md](docs/IMPORTING.md) for file layouts, Gmail setup and the full workflow.

Returns include capital gain, dividends, currency gain and total return as base-currency
amounts and simple period percentages, per holding, market and portfolio. Use `week`,
`month`, `ytd`, `all` (since first purchase), or an explicit date range. Holdings
sold during the period remain in the report; those sold earlier are omitted unless paid income. Missing
trading days and stored stale flags are reported; normal weekends and exchange
holidays do not make a price stale. Missing history stops the report rather than
producing a misleading zero. The command reads stored data;
it does not fetch prices. See [the calculation conventions and invented weekly example](docs/RETURNS.md)
for boundaries, FIFO attribution, dividend treatment and percentage denominators.

## Development

Needs Python 3.11 or newer, and a throwaway Postgres for the database tests.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check .
TEST_DATABASE_URL=postgresql+psycopg://postgres:pass@localhost:5432/test pytest
```

The tests **wipe** the database in `TEST_DATABASE_URL`, so never point it at real data. As a
safety net they refuse to run unless the database name contains `test`.

How we work on this repo: [docs/WORKING-AGREEMENT.md](docs/WORKING-AGREEMENT.md).

Akahu can confirm ledger holdings without changing trade history. See
[Akahu setup and drift](docs/AKAHU.md) and [adding a provider](docs/ADDING-A-PROVIDER.md).

### NZ tax-year summary

`--year 2026` means 1 April 2025 to 31 March 2026. Run a preview first:

```sh
portfolio-digest year-end --portfolio "Example" --year 2026 --dry-run --out ./private-preview
portfolio-digest year-end --portfolio "Example" --year 2026
```

The preview writes `year-end-2026.html` and `year-end-2026.csv` and sends nothing.
Keep these private financial reports outside git. The send command emails HTML with a
CSV attachment. Configure `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_SENDER` and
`SMTP_RECIPIENT` in the server's `.env`; authenticated servers also need `SMTP_USERNAME`
and `SMTP_PASSWORD`. SMTP always uses STARTTLS with certificate verification. Compose
passes these settings to the app. Preview mode needs no SMTP configuration.

The summary includes per-holding and portfolio realised gains and losses, gross dividends,
imputation credits, resident and foreign withholding, net received or reinvested, opening
and closing 31 March values, and total return. CSV retains full calculation precision;
HTML displays amounts to two decimal places. Realised sales use **historical FIFO cost**,
including acquisition fees and net of sale fees. Annual performance uses the existing
returns engine, which starts from opening market value and includes gross dividends.
Its percentage denominator is opening value plus purchase outlays. Splits preserve cost;
DRP units need a recorded buy or quantity adjustment as well as the income record.
Opening balances and transfers use their recorded value, so supply full acquisition
history for reliable historical cost. Missing prices or FX stop the report; carried-forward
data is disclosed in both outputs. This command never downloads prices or changes history.

The foreign cost figure is the peak historical NZD acquisition cost during the year,
including the opening position, rather than market value or just year-end cost. Same-day
additions precede sales because trade records have no execution times. All non-NZX holdings,
including Australian shares, are conservatively included. A warning appears **above
NZ$50,000**. FIF exemptions and calculations are outside this report; the threshold applies
per person across portfolios, so this portfolio alone cannot establish an exemption.

The current income schema has one combined withholding field. The report classifies it as
resident withholding for NZX holdings and foreign withholding otherwise. Check this
assumption against tax statements, particularly dual listings and NZ funds investing
overseas. Gross cash dividends exclude imputation credits, which are shown separately.
NZ generally does not tax capital gains for long-term investors, but exceptions apply.
This summary is information, not tax advice.

For automatic delivery, install the annual timer alongside the existing price timers:

```sh
cp deploy/systemd/portfolio-digest-year-end.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now portfolio-digest-year-end.timer
```

It runs `year-end --all` on **1 April at 09:00 Pacific/Auckland**, sending one email per
portfolio for the latest year ended 31 March. `Persistent=true` catches a missed run after
restart. The working directory follows the other units (`~/portfolio-digest`); adjust it
if needed. Ensure prices and FX through 31 March are available before running. Check
`journalctl --user -u portfolio-digest-year-end.service` for failures. Other portfolios
continue if one has missing data. Manual reruns send again, so check delivery before retrying
an uncertain SMTP result. For an all-portfolio preview, use `year-end --all --year 2026
--dry-run --out ./private-preview`; each portfolio gets a numbered subdirectory.

This checkout does not yet contain the #13 report/mailer, so the year-end email has a
self-contained, reusable SMTP boundary and a plain table style. Scheduling uses the existing
systemd convention. It reads dividend fields directly from the existing income records.
