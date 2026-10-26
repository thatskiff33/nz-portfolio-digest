from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from portfolio_digest import migrate
from portfolio_digest.db import session_scope
from portfolio_digest.models import (
    Base,
    FxRate,
    ImportRun,
    Income,
    Instrument,
    Money,
    Portfolio,
    Trade,
)

EXPECTED_TABLES = {
    "portfolios",
    "instruments",
    "import_runs",
    "trades",
    "income",
    "prices",
    "fx_rates",
}


def test_migrations_create_every_table(engine):
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_models_match_migrations(engine):
    """Fails if someone changes models.py without adding a migration."""
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_migrations_create_every_named_constraint_and_index(engine):
    """Autogenerate ignores CHECK constraints, so compare names to catch a missed one."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        expected = {c.name for c in table.constraints} | {i.name for i in table.indexes}
        actual = (
            {c["name"] for c in inspector.get_check_constraints(table.name)}
            | {c["name"] for c in inspector.get_unique_constraints(table.name)}
            | {c["name"] for c in inspector.get_foreign_keys(table.name)}
            | {i["name"] for i in inspector.get_indexes(table.name)}
            | {inspector.get_pk_constraint(table.name)["name"]}
        )
        assert actual == expected, table.name


def test_downgrade_then_upgrade_round_trips(engine):
    config = migrate.alembic_config(engine)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "base")
    assert EXPECTED_TABLES.isdisjoint(inspect(engine).get_table_names())
    migrate.upgrade(engine)
    assert migrate.current_revision(engine) == migrate.head_revision()


def _seed(session):
    portfolio = Portfolio(name="Personal")
    instrument = Instrument(exchange="NZX", code="AIR", name="Air New Zealand", currency="NZD")
    session.add_all([portfolio, instrument])
    session.flush()
    return portfolio, instrument


def _trade(portfolio, instrument, **overrides):
    values = {
        "portfolio_id": portfolio.id,
        "instrument_id": instrument.id,
        "trade_date": date(2026, 10, 1),
        "trade_type": "buy",
        "quantity": Decimal("10.5"),
        "price": Decimal("0.585"),
        "currency": "NZD",
        "source": "manual",
        "external_ref": "t1",
    }
    values.update(overrides)
    return Trade(**values)


def test_valid_trade_round_trips_exact_decimals(engine):
    with session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument, quantity=Decimal("0.12345678")))
    with session_scope(engine) as session:
        trade = session.query(Trade).one()
        assert trade.quantity == Decimal("0.12345678")
        assert trade.fees == Decimal(0)


def test_duplicate_broker_record_is_rejected(engine):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument))
        session.flush()
        session.add(_trade(portfolio, instrument))


@pytest.mark.parametrize(
    "overrides",
    [
        {"trade_type": "gift"},
        {"quantity": Decimal("-5")},
        {"quantity": Decimal("0"), "trade_type": "adjustment"},
        {"price": Decimal("-1")},
        {"fees": Decimal("-0.01")},
        {"currency": "nzd"},
        {"fx_rate": Decimal("0")},
    ],
)
def test_invalid_trades_are_rejected(engine, overrides):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument, **overrides))


def test_negative_adjustment_is_allowed(engine):
    with session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument, trade_type="adjustment", quantity=Decimal("-2")))


@pytest.mark.parametrize(
    ("exchange", "code"),
    [("nzx", "AIR"), ("NZX", "air"), ("NZX", ""), ("NZX", "A IR"), ("", "AIR")],
)
def test_instrument_codes_must_be_upper_case_without_spaces(engine, exchange, code):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(Instrument(exchange=exchange, code=code, name="x", currency="NZD"))


def test_fx_rate_cannot_convert_a_currency_to_itself(engine):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            FxRate(
                rate_date=date(2026, 10, 1),
                currency="NZD",
                base_currency="NZD",
                rate=Decimal(1),
                source="test",
            )
        )


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_portfolio_names_are_rejected(engine, name):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(Portfolio(name=name))


def test_portfolio_names_are_unique_ignoring_case(engine):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(Portfolio(name="Personal"))
        session.flush()
        session.add(Portfolio(name="PERSONAL"))


def test_deleting_a_portfolio_never_deletes_its_trades(engine):
    with session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument))
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.delete(session.get(Portfolio, portfolio.id))
    with session_scope(engine) as session:
        assert session.query(Trade).count() == 1


def test_deleting_an_import_run_never_orphans_its_trades(engine):
    with session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        run = ImportRun(portfolio_id=portfolio.id, source="manual")
        session.add(run)
        session.flush()
        session.add(_trade(portfolio, instrument, import_run_id=run.id))
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.delete(session.get(ImportRun, run.id))


@pytest.mark.parametrize(
    "overrides",
    [
        {"rows_imported": -1},
        {"content_sha256": "not-a-hash"},
        {"content_sha256": "A" * 64},
        {"status": "done"},
    ],
)
def test_invalid_import_runs_are_rejected(engine, overrides):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        portfolio, _ = _seed(session)
        session.add(ImportRun(portfolio_id=portfolio.id, source="manual", **overrides))


def _income(portfolio, instrument, **overrides):
    values = {
        "portfolio_id": portfolio.id,
        "instrument_id": instrument.id,
        "paid_date": date(2026, 10, 1),
        "currency": "NZD",
        "gross_amount": Decimal("100.00"),
        "net_amount": Decimal("67.00"),
        "withholding_tax": Decimal("33.00"),
        "imputation_credits": Decimal("38.89"),
        "source": "manual",
        "external_ref": "d1",
    }
    values.update(overrides)
    return Income(**values)


def test_valid_income_is_accepted(engine):
    with session_scope(engine) as session:
        session.add(_income(*_seed(session)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"net_amount": Decimal("100.01")},
        {"withholding_tax": Decimal("100.01")},
        {"drp_quantity": Decimal("0")},
        {"kind": "bonus"},
    ],
)
def test_invalid_income_is_rejected(engine, overrides):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(_income(*_seed(session), **overrides))


NUMERIC_COLUMNS = [
    (table.name, column.name)
    for table in Base.metadata.sorted_tables
    for column in table.columns
    if isinstance(column.type, type(Money))
]


def test_every_numeric_column_is_guarded_against_nan():
    guarded = {
        (table.name, constraint.name.removeprefix(f"ck_{table.name}_").removesuffix("_not_nan"))
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if constraint.name and constraint.name.endswith("_not_nan")
    }
    assert set(NUMERIC_COLUMNS) <= guarded


@pytest.mark.parametrize(("table", "column"), NUMERIC_COLUMNS)
def test_nan_is_refused_by_the_database(engine, table, column):
    """Postgres sorts NaN above every number, so `>= 0` checks alone let it through."""
    with session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument))
        session.add(_income(portfolio, instrument))
        session.add(
            FxRate(
                rate_date=date(2026, 10, 1),
                currency="AUD",
                base_currency="NZD",
                rate=Decimal("1.1"),
                source="test",
            )
        )
        session.execute(
            text(
                "INSERT INTO prices (instrument_id, price_date, close, source) "
                "VALUES (:i, '2026-10-01', 1, 'test')"
            ),
            {"i": instrument.id},
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        # Table and column names come from our own metadata, not from input.
        conn.execute(text(f"UPDATE {table} SET {column} = 'NaN'"))  # noqa: S608


@pytest.mark.parametrize("value", [0.1, float("nan"), Decimal("NaN"), Decimal("Infinity")])
def test_floats_and_non_finite_amounts_never_reach_the_database(engine, value):
    with pytest.raises(StatementError), session_scope(engine) as session:
        portfolio, instrument = _seed(session)
        session.add(_trade(portfolio, instrument, price=value))
