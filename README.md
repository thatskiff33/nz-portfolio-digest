# nz-portfolio-digest

A self-hosted share portfolio tracker for New Zealand investors. It imports trades
straight from your brokers and share registries, values **every** holding in NZ$ each day,
and sends you a performance email (plus a private dashboard) on the schedule you choose.

Built to replace the Sharesight free plan, which only shows 10 holdings.

## Running it

See [docs/DEPLOY.md](docs/DEPLOY.md) for the Docker server workflow. The installed CLI
examples below are for local development with a reachable database.

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
[shared price and report schedule](docs/DEPLOY.md#shared-price-and-report-schedule)
(11:15 and 18:30 Pacific/Auckland) and recovery commands. The morning fetch follows
the US price and FX closes in every daylight-saving combination. Stored closes are raw,
unadjusted prices in major currency units. Registry-recorded splits and consolidations
adjust units without changing total cost or parcel FX. Holdings round to 8 decimal places,
half-even, at the action; record any cash in lieu separately as a sale. Matching registry actions
and ADJUSTMENT entries within inclusive ±10 calendar days are refused in either order.
For downloaded Split metadata, an ADJUSTMENT trade records the quantity change; the
returns engine preserves parcel cost when it matches that ratio exactly. Missing
adjustments produce an actionable warning in returns, emails and `prices check`, and
suppress the portfolio-drop alert until resolved. Other adjustments
move units at their recorded price without cash. Do not split-adjust stored closes again.

Dividends and ETF distributions import from Sharesies CSV and ASB dividend advice. Yahoo
can suggest missing dividends, which stay outside returns until explicitly confirmed.
`portfolio-digest returns --portfolio Personal --period all` reports net dividends, gross
income, tax and a separate imputation column; `--gross-up` includes NZ imputation in totals.
See [dividends and corporate actions](docs/dividends.md) for tax fields, DRP, suggestions,
rights issues and instrument history.

The app also reads ASB Securities contract notes from one Gmail label with read-only
access, and supports manual corrections, gifted shares and opening balances. See
[docs/IMPORTING.md](docs/IMPORTING.md) for file layouts, Gmail setup and the full workflow.

Returns include capital gain, dividends, currency gain and total return as base-currency
amounts and simple period percentages, per holding, market and portfolio. Use `week`,
`month`, `ytd`, `all` (since first purchase), or an explicit date range. Holdings
sold during the period remain in the report. Those sold earlier remain when they paid
income during the period. Missing trading days and stored stale flags are reported; normal weekends and exchange
holidays do not make a price stale. Missing history stops the report rather than
producing a misleading zero. The command reads stored data;
it does not fetch prices. See [the calculation conventions and invented weekly example](docs/RETURNS.md)
for boundaries, FIFO attribution, dividend treatment and percentage denominators.

See the [NZ tax-year summary](docs/YEAR-END.md) for annual reports and tax caveats,
and [price and movement alerts](docs/ALERTS.md) for notifications.

Akahu can confirm ledger holdings without changing trade history. See
[Akahu setup and drift](docs/AKAHU.md) and [adding a provider](docs/ADDING-A-PROVIDER.md).

## Development

Needs Python 3.11 or newer, uv, and a throwaway Postgres for the database tests.
`uv sync --frozen` installs the versions in `uv.lock`, which is what CI and the server image use.

```sh
uv sync --frozen --extra dev
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql+psycopg://postgres:pass@localhost:5432/test uv run pytest
```

PowerShell:

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://postgres:pass@localhost:5432/test"
.venv\Scripts\pytest
```

Database tests skip without `TEST_DATABASE_URL`. Set `REQUIRE_TEST_DB=1` to fail instead
when `TEST_DATABASE_URL` is missing. Backup and deployment script tests need a POSIX
shell, such as WSL on Windows.

The tests **wipe** the database in `TEST_DATABASE_URL`, so never point it at real data. As a
safety net they refuse to run unless the database name contains `test`.

How we work on this repo: [docs/WORKING-AGREEMENT.md](docs/WORKING-AGREEMENT.md).
