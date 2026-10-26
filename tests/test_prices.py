from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from portfolio_digest.db import session_scope
from portfolio_digest.models import FxRate, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.fetch import (
    Sources,
    carry_forward,
    fetch_start,
    held_instruments,
    run_fetch,
)
from portfolio_digest.prices.http import SourceError
from portfolio_digest.prices.quality import check_data
from portfolio_digest.prices.sources import Listing, PriceHistory


def test_normal_fetch_rechecks_recent_history_but_backfill_range_starts_at_first_trade():
    first = date(2020, 1, 1)
    latest = date(2026, 10, 2)
    assert fetch_start(first, None, latest) == first
    assert fetch_start(first, date(2026, 9, 30), latest) == date(2026, 9, 1)
    assert fetch_start(first, date(2020, 1, 2), latest) == date(2020, 1, 3)


def test_missing_values_are_carried_only_after_a_known_value():
    days = [date(2026, 10, day) for day in range(1, 6)]
    assert carry_forward(days, {days[1]: Decimal("10"), days[4]: Decimal("12")}, None) == {
        days[2]: Decimal("10"),
        days[3]: Decimal("10"),
    }


class FakePriceSource:
    name = "mock-price"

    def __init__(self, closes=None, splits=None):
        self.closes = closes or {}
        self.splits = splits or {}
        self.calls = []

    def symbol(self, listing: Listing) -> str:
        return f"{listing.code}.TEST"

    def history(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return PriceHistory(dict(self.closes), dict(self.splits))


class FakeFxSource:
    def __init__(self, name, rates=None, error=None):
        self.name = name
        self.result = rates or {}
        self.error = error
        self.calls = []

    def rates(self, currency, base, start, end):
        self.calls.append((currency, base, start, end))
        if self.error:
            raise SourceError(self.error)
        return dict(self.result)


def _seed_trade(engine, *, currency="USD", code="TEST"):
    with session_scope(engine) as session:
        portfolio = Portfolio(name="Personal")
        instrument = Instrument(
            exchange="NASDAQ", code=code, name="Made-up Limited", currency=currency
        )
        session.add_all([portfolio, instrument])
        session.flush()
        session.add(
            Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=date(2026, 10, 1),
                trade_type="buy",
                quantity=Decimal("5"),
                price=Decimal("10"),
                currency=currency,
                source="test",
                external_ref=f"trade-{code}",
            )
        )
        return instrument.id


def test_backfill_uses_fallback_fx_carries_missing_days_and_is_idempotent(engine):
    instrument_id = _seed_trade(engine)
    prices = FakePriceSource({date(2026, 10, 1): Decimal("10")})
    yahoo_fx = FakeFxSource("yahoo", error="temporarily unavailable")
    frankfurter = FakeFxSource("frankfurter", {date(2026, 10, 1): Decimal("1.7")})
    sources = Sources(prices, [yahoo_fx, frankfurter])
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)

    first = run_fetch(engine, sources, now=now, base_currency="NZD", backfill=True)
    assert first.ok
    assert prices.calls[0][1:] == (date(2026, 10, 1), date(2026, 10, 2))
    fx_outcome = next(item for item in first.outcomes if item.label == "USD/NZD")
    assert fx_outcome.source == "frankfurter"
    assert fx_outcome.fallbacks and "yahoo failed" in fx_outcome.fallbacks[0]
    assert fx_outcome.carried_forward == 1

    prices.closes = {date(2026, 10, 1): Decimal("11")}
    second = run_fetch(engine, sources, now=now, base_currency="NZD", backfill=True)
    assert second.ok
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Price)) == 2
        assert session.scalar(select(func.count()).select_from(FxRate)) == 2
        saved = session.get(Price, (instrument_id, date(2026, 10, 1)))
        stale = session.get(Price, (instrument_id, date(2026, 10, 2)))
        assert saved.close == Decimal("11.000000") and not saved.is_stale
        assert stale.close == Decimal("11.000000") and stale.is_stale
        findings = check_data(session, held_instruments(session), "NZD", now)
        assert {(item.kind, item.day) for item in findings} == {
            ("stale-price", date(2026, 10, 2)),
            ("stale-fx", date(2026, 10, 2)),
        }


def test_fx_fallback_fills_a_partial_primary_response(engine):
    _seed_trade(engine)
    prices = FakePriceSource({date(2026, 10, 1): Decimal("10")})
    yahoo_fx = FakeFxSource("yahoo", {date(2026, 10, 1): Decimal("1.7")})
    frankfurter = FakeFxSource("frankfurter", {date(2026, 10, 2): Decimal("1.8")})

    report = run_fetch(
        engine,
        Sources(prices, [yahoo_fx, frankfurter]),
        now=datetime(2026, 10, 3, 12, tzinfo=UTC),
        base_currency="NZD",
        backfill=True,
    )

    outcome = next(item for item in report.outcomes if item.label == "USD/NZD")
    assert outcome.source == "yahoo, frankfurter"
    assert outcome.fetched == 2
    assert outcome.carried_forward == 0
    assert len(frankfurter.calls) == 1
    with session_scope(engine) as session:
        saved = session.scalars(select(FxRate).order_by(FxRate.rate_date)).all()
        assert [(row.rate, row.source, row.is_stale) for row in saved] == [
            (Decimal("1.7000000000"), "yahoo", False),
            (Decimal("1.8000000000"), "frankfurter", False),
        ]


def test_one_portfolios_short_position_does_not_cancel_anothers_holding(engine):
    instrument_id = _seed_trade(engine, currency="NZD", code="OFFSET")
    with session_scope(engine) as session:
        other = Portfolio(name="Other")
        session.add(other)
        session.flush()
        session.add(
            Trade(
                portfolio_id=other.id,
                instrument_id=instrument_id,
                trade_date=date(2026, 10, 1),
                trade_type="adjustment",
                quantity=Decimal("-5"),
                price=Decimal("0"),
                currency="NZD",
                source="test",
                external_ref="other-offset",
            )
        )
    source = FakePriceSource({date(2026, 10, 1): Decimal("1")})
    run_fetch(
        engine,
        Sources(source, []),
        now=datetime(2026, 10, 3, 12, tzinfo=UTC),
        base_currency="NZD",
        backfill=True,
    )
    assert source.calls[0][1:] == (date(2026, 10, 1), date(2026, 10, 2))


def test_quality_checks_find_missing_zero_and_jump_but_exempt_a_split(engine):
    instrument_id = _seed_trade(engine, currency="NZD", code="QUALITY")
    with session_scope(engine) as session:
        session.add_all(
            [
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 1),
                    close=Decimal("10"),
                    source="test",
                ),
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 2),
                    close=Decimal("0"),
                    source="test",
                ),
            ]
        )
    now = datetime(2026, 10, 6, 22, tzinfo=UTC)
    with session_scope(engine) as session:
        findings = check_data(session, held_instruments(session), "NZD", now)
    assert {(item.kind, item.day) for item in findings} == {
        ("zero-price", date(2026, 10, 2)),
        ("missing-price", date(2026, 10, 5)),
        ("missing-price", date(2026, 10, 6)),
    }

    with session_scope(engine) as session:
        session.add_all(
            [
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 5),
                    close=Decimal("4"),
                    source="test",
                ),
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 6),
                    close=Decimal("10"),
                    source="test",
                ),
            ]
        )
    with session_scope(engine) as session:
        findings = check_data(session, held_instruments(session), "NZD", now)
    assert [(item.kind, item.day) for item in findings if item.kind == "suspicious-jump"] == [
        ("suspicious-jump", date(2026, 10, 6))
    ]

    with session_scope(engine) as session:
        session.add(
            Split(
                instrument_id=instrument_id,
                split_date=date(2026, 10, 6),
                shares_before=Decimal("1"),
                shares_after=Decimal("2"),
                source="test",
            )
        )
    with session_scope(engine) as session:
        findings = check_data(session, held_instruments(session), "NZD", now)
    assert not [item for item in findings if item.kind == "suspicious-jump"]


def test_quality_check_does_not_call_a_multi_day_move_a_one_day_jump(engine):
    instrument_id = _seed_trade(engine, currency="NZD", code="STALEGAP")
    with session_scope(engine) as session:
        session.add_all(
            [
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 1),
                    close=Decimal("10"),
                    source="test",
                ),
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 2),
                    close=Decimal("10"),
                    source="test",
                    is_stale=True,
                ),
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 5),
                    close=Decimal("10"),
                    source="test",
                    is_stale=True,
                ),
                Price(
                    instrument_id=instrument_id,
                    price_date=date(2026, 10, 6),
                    close=Decimal("16"),
                    source="test",
                ),
            ]
        )
    now = datetime(2026, 10, 6, 22, tzinfo=UTC)
    with session_scope(engine) as session:
        findings = check_data(session, held_instruments(session), "NZD", now)
    assert not [item for item in findings if item.kind == "suspicious-jump"]
