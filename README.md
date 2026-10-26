# nz-portfolio-digest

A self-hosted share portfolio tracker for New Zealand investors. It imports trades
straight from your brokers and share registries, values **every** holding in NZ$ each day,
and sends you a performance email (plus a private dashboard) on the schedule you choose.

Built to replace the Sharesight free plan, which only shows 10 holdings.

## Status

Under construction. The plan and progress are tracked in the Epic issue (#10).

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
portfolio-digest portfolio add "Personal"
portfolio-digest portfolio list
```

## Development

Needs Python 3.11 or newer, and a throwaway Postgres for the database tests.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check .
TEST_DATABASE_URL=postgresql+psycopg://postgres:pass@localhost:5432/test pytest
```

The tests **wipe** the database in `TEST_DATABASE_URL`, so never point it at real data.

How we work on this repo: [docs/WORKING-AGREEMENT.md](docs/WORKING-AGREEMENT.md).
