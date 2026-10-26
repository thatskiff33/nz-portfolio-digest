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
portfolio-digest returns --portfolio "Personal" --period week
portfolio-digest returns --portfolio "Personal" --period 2026-10-01:2026-10-26
```

Returns include capital gain, dividends, currency gain and total return as base-currency
amounts and simple period percentages, per holding, market and portfolio. Use `week`,
`month`, `ytd`, `all` (since first purchase), or an explicit date range. Sold holdings
remain in the report; carried-forward prices and FX are flagged. Missing history stops
the report rather than producing a misleading zero. The command reads stored data;
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
