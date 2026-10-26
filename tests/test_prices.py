from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

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
from portfolio_digest.prices.http import JsonFetcher, SourceError
from portfolio_digest.prices.quality import check_data
from portfolio_digest.prices.sources import Listing, PriceHistory, YahooSource


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


@pytest.mark.parametrize("run_hour", [21, 5])
def test_bad_content_encoding_does_not_stop_second_holding_or_fx(monkeypatch, run_hour):
    from portfolio_digest import db
    from portfolio_digest.prices import fetch

    first = date(2026, 10, 1)
    holdings = [
        Holding(i, "NASDAQ", code, "USD", None, first, first, True)
        for i, code in enumerate(["AAA", "BBB"], 1)
    ]
    sessions = [MagicMock() for _ in range(4)]
    for session in sessions:
        session.scalar.return_value = None
        session.execute.return_value.all.return_value = []
    factory = MagicMock(side_effect=sessions)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(fetch, "held_instruments", lambda *args: holdings)
    for name in ("_save_prices", "_save_rates", "_save_splits"):
        monkeypatch.setattr(fetch, name, MagicMock())
    calls = []

    def handler(request):
        symbol = request.url.path.rsplit("/", 1)[-1]
        calls.append(symbol)
        if symbol == "AAA":
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"not a gzip body"),
            )
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "exchangeTimezoneName": "UTC",
                                "currency": "NZD" if "=X" in symbol else "USD",
                            },
                            "timestamp": [int(datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp())],
                            "indicators": {"quote": [{"close": [2]}]},
                        }
                    ]
                }
            },
        )

    waits = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        yahoo = YahooSource(JsonFetcher(client, attempts=2, backoff=1, sleep=waits.append))
        report = run_fetch(
            MagicMock(),
            Sources(yahoo, [yahoo]),
            now=datetime(2026, 10, 3, run_hour, 30, tzinfo=UTC),
            base_currency="NZD",
            backfill=True,
        )

    assert not report.ok
    assert report.outcomes[0].error == (
        "DecodingError from query2.finance.yahoo.com, gave up after 2 attempts."
    )
    assert all(outcome.error is None for outcome in report.outcomes[1:])
    assert [outcome.fetched for outcome in report.outcomes] == [0, 1, 1]
    assert calls == ["AAA", "AAA", "BBB", "USDNZD=X"]
    assert waits == [1]


@pytest.mark.parametrize(
    ("options", "last_good", "still_held", "now", "should_fetch"),
    [
        ({}, date(2023, 3, 3), False, datetime(2026, 10, 3, 12, tzinfo=UTC), False),
        ({"backfill": True}, date(2023, 3, 3), False, datetime(2026, 10, 3, 12, tzinfo=UTC), True),
        (
            {"start": date(2023, 3, 1)},
            date(2023, 3, 3),
            False,
            datetime(2026, 10, 3, 12, tzinfo=UTC),
            True,
        ),
        (
            {"only": ["NASDAQ:OLD"]},
            date(2023, 3, 3),
            False,
            datetime(2026, 10, 3, 12, tzinfo=UTC),
            True,
        ),
        ({}, date(2023, 3, 2), False, datetime(2026, 10, 3, 12, tzinfo=UTC), True),
        ({}, None, False, datetime(2026, 10, 3, 12, tzinfo=UTC), True),
        ({}, date(2023, 3, 3), False, datetime(2023, 4, 3, 12, tzinfo=UTC), True),
        ({}, date(2023, 3, 3), False, datetime(2023, 4, 4, 12, tzinfo=UTC), False),
        ({}, date(2023, 3, 3), True, datetime(2026, 10, 3, 12, tzinfo=UTC), True),
    ],
)
def test_old_sold_holdings_and_fx_skip_only_when_complete(
    monkeypatch, options, last_good, still_held, now, should_fetch
):
    from portfolio_digest import db
    from portfolio_digest.prices import fetch

    holding = Holding(
        1, "NASDAQ", "OLD", "USD", None, date(2023, 3, 1), date(2023, 3, 3), still_held
    )
    sessions = [MagicMock() for _ in range(3)]
    for session in sessions:
        session.execute.return_value.all.return_value = []
    inactive_check = (
        not (still_held or options)
        and last_good is not None
        and (now.date() - holding.last_trade).days > 31
    )
    terminal = [last_good if last_good == holding.last_trade else None] if inactive_check else []
    sessions[1].scalar.side_effect = [last_good, *terminal, last_good, None]
    sessions[2].scalar.side_effect = [last_good, *terminal, None]
    factory = MagicMock(side_effect=sessions)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(fetch, "held_instruments", lambda *args: [holding])
    for name in ("_save_prices", "_save_rates", "_save_splits"):
        monkeypatch.setattr(fetch, name, MagicMock())
    prices = FakePriceSource()
    prices.history = MagicMock(side_effect=SourceError("symbol no longer available (HTTP 404)"))
    fx = FakeFxSource("mock-fx", error="currency no longer available")

    report = run_fetch(MagicMock(), Sources(prices, [fx]), now=now, base_currency="NZD", **options)

    assert prices.history.called is should_fetch
    assert bool(fx.calls) is should_fetch
    assert report.ok is not should_fetch
    assert len(report.outcomes) == 2


@pytest.mark.parametrize("run_hour", [21, 5])
@pytest.mark.parametrize("terminal_present", [False, True])
@pytest.mark.parametrize("fresh_present", [False, True])
def test_takeover_skips_prices_and_sold_only_fx_with_a_carried_final_value(
    monkeypatch, run_hour, terminal_present, fresh_present
):
    from portfolio_digest import db
    from portfolio_digest.prices import fetch

    first, last_close, sale = date(2023, 3, 1), date(2023, 3, 3), date(2023, 3, 8)
    holding = Holding(1, "NZX", "OLD", "AUD", None, first, sale, False)
    sessions = [MagicMock() for _ in range(3)]
    last_good = last_close if fresh_present else None
    terminal = [sale if terminal_present else None] if fresh_present else []
    for session in sessions:
        session.execute.return_value.all.return_value = []
    sessions[1].scalar.side_effect = [last_good, *terminal, first, None]
    sessions[2].scalar.side_effect = [last_good, *terminal, None]
    factory = MagicMock(side_effect=sessions)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(fetch, "held_instruments", lambda *args: [holding])
    for name in ("_save_prices", "_save_rates", "_save_splits"):
        monkeypatch.setattr(fetch, name, MagicMock())
    prices = FakePriceSource()
    prices.history = MagicMock(side_effect=SourceError("symbol no longer available (HTTP 404)"))
    fx = FakeFxSource("mock-fx", error="currency no longer available")

    report = run_fetch(
        MagicMock(),
        Sources(prices, [fx]),
        now=datetime(2026, 10, 3, run_hour, 30, tzinfo=UTC),
        base_currency="NZD",
    )

    complete = terminal_present and fresh_present
    assert report.ok is complete
    assert prices.history.called is not complete
    assert bool(fx.calls) is not complete
    assert len(report.outcomes) == 2


def test_takeover_with_stale_rows_through_sale_does_not_refetch_delisted_symbol(engine):
    first, sale = date(2023, 3, 1), date(2023, 3, 8)
    instrument_id = _seed_trade(engine, currency="AUD", code="TAKEOVER")
    with session_scope(engine) as session:
        session.get(Instrument, instrument_id).exchange = "NZX"
        buy = session.scalar(select(Trade).where(Trade.instrument_id == instrument_id))
        buy.trade_date = first
        session.add(
            Trade(
                portfolio_id=buy.portfolio_id,
                instrument_id=instrument_id,
                trade_date=sale,
                trade_type="sell",
                quantity=Decimal("5"),
                price=Decimal("10"),
                currency="AUD",
                source="test",
                external_ref="takeover-sale",
            )
        )
    closes = {date(2023, 3, day): Decimal("10") for day in range(1, 4)}
    rates = {day: Decimal("1.1") for day in closes}
    prices, fx = FakePriceSource(closes), FakeFxSource("mock-fx", rates)
    now = datetime(2026, 10, 3, 5, 30, tzinfo=UTC)
    report = run_fetch(engine, Sources(prices, [fx]), now=now, base_currency="NZD", backfill=True)
    assert report.ok
    assert [outcome.carried_forward for outcome in report.outcomes] == [3, 3]
    with session_scope(engine) as session:
        assert session.get(Price, (instrument_id, sale)).is_stale
        assert session.get(FxRate, (sale, "AUD", "NZD")).is_stale
        # Later stored rows must not hide the completed sale-day range.
        session.add(
            Price(
                instrument_id=instrument_id,
                price_date=date(2023, 3, 9),
                close=Decimal("11"),
                source="test",
            )
        )
        session.add(
            FxRate(
                rate_date=date(2023, 3, 9),
                currency="AUD",
                base_currency="NZD",
                rate=Decimal("1.2"),
                source="test",
            )
        )
    prices.history = MagicMock(side_effect=SourceError("symbol no longer available (HTTP 404)"))
    fx.calls.clear()
    fx.error = "currency no longer available"
    for run_hour in (21, 5):
        report = run_fetch(
            engine,
            Sources(prices, [fx]),
            now=now.replace(hour=run_hour),
            base_currency="NZD",
        )
        assert report.ok
        assert all(outcome.fetched == outcome.carried_forward == 0 for outcome in report.outcomes)
    prices.history.assert_not_called()
    assert not fx.calls


def test_carried_price_overflow_does_not_stop_next_instrument_or_fx(monkeypatch):
    from portfolio_digest import db
    from portfolio_digest.prices import fetch

    first, second = date(2026, 10, 1), date(2026, 10, 2)
    holdings = [
        Holding(i, "NASDAQ", code, "USD", None, first, first, True)
        for i, code in enumerate(["AAA", "BBB"], 1)
    ]
    sessions = [MagicMock() for _ in range(4)]
    for session, value in zip(sessions[1:], ["50", "10", "1.7"], strict=True):
        session.scalar.side_effect = [first, first, None] if value != "1.7" else [None, None]
        rows = MagicMock()
        rows.all.return_value = [(first, Decimal(value))]
        session.execute.side_effect = [[], rows] if value != "1.7" else [rows]
    factory = MagicMock(side_effect=sessions)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(fetch, "held_instruments", lambda *args: holdings)
    monkeypatch.setattr(fetch, "_save_splits", MagicMock())
    save_prices, save_rates = MagicMock(), MagicMock()
    monkeypatch.setattr(fetch, "_save_prices", save_prices)
    monkeypatch.setattr(fetch, "_save_rates", save_rates)
    prices = FakePriceSource()
    prices.history = MagicMock(
        side_effect=[
            PriceHistory({}, {second: (Decimal("1e13"), Decimal("1"))}),
            PriceHistory({first: Decimal("10")}, {}),
        ]
    )
    fx = FakeFxSource("mock-fx", {first: Decimal("1.7"), second: Decimal("1.8")})

    report = run_fetch(
        MagicMock(),
        Sources(prices, [fx]),
        now=datetime(2026, 10, 3, 12, tzinfo=UTC),
        base_currency="NZD",
        backfill=True,
    )

    assert not report.ok
    assert "out-of-range carried price for 2026-10-02" in report.outcomes[0].error
    assert report.outcomes[0].carried_forward == 0
    assert all(outcome.error is None for outcome in report.outcomes[1:])
    assert report.outcomes[1].fetched == 1
    assert report.outcomes[2].fetched == 2
    assert save_prices.call_args_list[1].args[2] == {}
    assert save_prices.call_args_list[3].args[2] == {second: Decimal("10")}
    assert save_rates.call_args_list[0].args[2] == fx.result
    for session in sessions:
        session.commit.assert_called_once()
        session.rollback.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["execute", "commit"])
@pytest.mark.parametrize("failed_item", ["instrument", "currency"])
def test_database_failure_rolls_back_and_continues(monkeypatch, failure_stage, failed_item):
    from portfolio_digest import db
    from portfolio_digest.prices import fetch

    first = date(2026, 10, 1)
    holdings = [
        Holding(i, "NASDAQ", code, currency, None, first, first, True)
        for i, (code, currency) in enumerate([("AAA", "AUD"), ("BBB", "USD")], 1)
    ]
    sessions = [MagicMock() for _ in range(5)]
    failed_index = 1 if failed_item == "instrument" else 3
    factory = MagicMock(side_effect=sessions)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(fetch, "held_instruments", lambda *args: holdings)

    def update(session, source, item, *args):
        if failure_stage == "execute" and session is sessions[failed_index]:
            raise SQLAlchemyError("private SQL and parameters")
        return fetch.Outcome(item.label, fetched=1)

    monkeypatch.setattr(fetch, "_update_instrument", update)
    monkeypatch.setattr(fetch, "_update_currency", update)
    if failure_stage == "commit":
        sessions[failed_index].commit.side_effect = SQLAlchemyError("private SQL and parameters")

    report = run_fetch(
        MagicMock(),
        Sources(FakePriceSource(), []),
        now=datetime(2026, 10, 3, 12, tzinfo=UTC),
        base_currency="NZD",
    )

    assert not report.ok
    assert len(report.outcomes) == 4
    failed = report.outcomes[failed_index - 1]
    assert failed.error == "database error; changes rolled back"
    assert failed.fetched == failed.carried_forward == 0
    assert all(outcome.error is None for outcome in report.outcomes if outcome is not failed)
    sessions[failed_index].rollback.assert_called_once()
    for index, session in enumerate(sessions):
        if index != failed_index:
            session.commit.assert_called_once()
            session.rollback.assert_not_called()


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
