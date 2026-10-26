from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from portfolio_digest.db import session_scope
from portfolio_digest.models import FxRate, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.fetch import (
    Holding,
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

    def history(self, symbol, start, end, *, currency=None, split_start=None):
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


@pytest.mark.parametrize("gap", ["stale", "missing"])
@pytest.mark.parametrize(
    "split_day", [None, date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
)
def test_quality_compares_fresh_closes_across_gaps_unless_a_split_intervenes(gap, split_day):
    first, middle, last = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)
    rows = [(first, Decimal("10"), False)]
    if gap == "stale":
        rows.append((middle, Decimal("10"), True))
    rows.append((last, Decimal("100"), False))
    session = MagicMock()
    session.execute.return_value.all.return_value = rows
    session.scalars.return_value = [] if split_day is None else [split_day]
    holding = Holding(1, "NZX", "QUALITY", "NZD", None, first, last, False)
    findings = check_data(session, [holding], "NZD", datetime(2026, 10, 6, 22, tzinfo=UTC))
    assert ("stale-price" if gap == "stale" else "missing-price", middle) in {
        (item.kind, item.day) for item in findings
    }
    jumps = [item for item in findings if item.kind == "suspicious-jump"]
    if split_day is None or split_day == first:
        assert len(jumps) == 1
        assert jumps[0].day == last
        assert jumps[0].detail == "close changed 900.0% since 2026-10-01 without a split"
    else:
        assert not jumps


def test_new_split_refreshes_full_stored_history_once(monkeypatch):
    from unittest.mock import MagicMock

    from portfolio_digest.prices import fetch

    first, recent, split_day = date(2026, 8, 3), date(2026, 10, 1), date(2026, 10, 2)
    holding = fetch.Holding(1, "NASDAQ", "TEST", "USD", None, first, first, True)
    session = MagicMock()
    source = FakePriceSource(
        {first: Decimal("40"), recent: Decimal("40"), split_day: Decimal("10")},
        {split_day: (Decimal("1"), Decimal("4"))},
    )
    saved = []
    monkeypatch.setattr(fetch, "_save_prices", lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(fetch, "_save_splits", lambda *args: None)

    def run(recorded):
        session.scalar.side_effect = [recent, first, None]
        split_rows = MagicMock()
        split_rows.__iter__.return_value = iter(recorded)
        existing_rows = MagicMock()
        existing_rows.all.return_value = list(source.closes.items())
        session.execute.side_effect = [split_rows, existing_rows]
        return fetch._update_instrument(
            session, source, holding, datetime(2026, 10, 3, 12, tzinfo=UTC), False, None
        )

    assert run([]).error is None
    assert [call[1] for call in source.calls] == [date(2026, 9, 1), first]
    assert saved[0][0][2][first] == Decimal("40")
    source.calls.clear()
    assert run([(split_day, Decimal("1"), Decimal("4"))]).error is None
    assert [call[1] for call in source.calls] == [date(2026, 9, 1)]


def test_raw_carry_forward_changes_units_on_split_date():
    first, split_day, next_day = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)
    splits = {split_day: (Decimal("1"), Decimal("4"))}
    assert carry_forward(
        [first, split_day, next_day],
        {first: Decimal("40")},
        None,
        splits=splits,
    ) == {split_day: Decimal("10"), next_day: Decimal("10")}
    # A real split-day close is already in the new units and replaces the carry.
    assert carry_forward(
        [split_day, next_day],
        {split_day: Decimal("11")},
        Decimal("40"),
        splits=splits,
    ) == {next_day: Decimal("11")}


def test_withdrawn_split_refreshes_history_and_does_not_scale_stale_prices(monkeypatch):
    from unittest.mock import MagicMock

    from portfolio_digest.prices import fetch

    first, recent, split_day = date(2026, 8, 3), date(2026, 10, 1), date(2026, 10, 2)
    holding = fetch.Holding(1, "NASDAQ", "TEST", "USD", None, first, first, True)
    source = FakePriceSource({recent: Decimal("40")})
    session = MagicMock()
    session.scalar.side_effect = [recent, first, None]
    session.execute.side_effect = [
        [(split_day, Decimal("1"), Decimal("4"))],
        MagicMock(all=lambda: [(recent, Decimal("40"))]),
    ]
    saved, scans = [], []
    monkeypatch.setattr(fetch, "_save_prices", lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(fetch, "_save_splits", lambda *args: scans.append(args))
    result = fetch._update_instrument(
        session, source, holding, datetime(2026, 10, 3, 12, tzinfo=UTC), False, None
    )
    assert result.error is None
    assert [call[1] for call in source.calls] == [date(2026, 9, 1), first]
    assert scans[0][2] == {}
    assert scans[0][4] == first
    assert saved[-1][0][2][split_day] == Decimal("40")


def test_split_scan_removes_withdrawn_or_moved_rows(engine):
    from portfolio_digest.prices.fetch import _save_splits

    instrument_id = _seed_trade(engine)
    first, old_day, moved_day = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)
    ratio = (Decimal("1"), Decimal("4"))
    with session_scope(engine) as session:
        _save_splits(session, instrument_id, {old_day: ratio}, "yahoo", first)
        _save_splits(session, instrument_id, {moved_day: ratio}, "yahoo", first)
        rows = session.scalars(select(Split).where(Split.instrument_id == instrument_id)).all()
        assert [(row.split_date, row.shares_after) for row in rows] == [(moved_day, Decimal("4"))]
        _save_splits(session, instrument_id, {}, "yahoo", first)
        assert session.scalars(select(Split)).all() == []


def test_failed_split_refresh_preserves_recorded_events(monkeypatch):
    from unittest.mock import MagicMock

    from portfolio_digest.prices import fetch

    first, recent, split_day = date(2026, 8, 3), date(2026, 10, 1), date(2026, 10, 2)
    holding = fetch.Holding(1, "NASDAQ", "TEST", "USD", None, first, first, True)
    source = MagicMock(name="yahoo")
    source.history.side_effect = [PriceHistory(), SourceError("refresh failed")]
    session = MagicMock()
    session.scalar.side_effect = [recent, first, None]
    session.execute.side_effect = [
        [(split_day, Decimal("1"), Decimal("4"))],
        MagicMock(all=lambda: [(recent, Decimal("40"))]),
    ]
    save_splits = MagicMock()
    saved = []
    monkeypatch.setattr(fetch, "_save_splits", save_splits)
    monkeypatch.setattr(fetch, "_save_prices", lambda *args, **kwargs: saved.append((args, kwargs)))
    result = fetch._update_instrument(
        session, source, holding, datetime(2026, 10, 3, 12, tzinfo=UTC), False, None
    )
    assert result.error == "refresh failed"
    save_splits.assert_not_called()
    assert saved[-1][0][2][split_day] == Decimal("10")
