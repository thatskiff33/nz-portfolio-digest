from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from portfolio_digest import migrate
from portfolio_digest.db import session_scope
from portfolio_digest.models import Base, FxRate, Instrument, Portfolio, Trade

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
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == []


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


def test_instrument_codes_must_be_upper_case(engine):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(Instrument(exchange="nzx", code="air", name="x", currency="NZD"))


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
