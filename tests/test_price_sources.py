from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from portfolio_digest.prices.http import JsonFetcher, SourceError
from portfolio_digest.prices.sources import FrankfurterSource, Listing, YahooSource
from portfolio_digest.prices.symbols import UnknownSymbol, clean_symbol, yahoo_symbol


@pytest.mark.parametrize(
    ("exchange", "code", "expected"),
    [
        ("NZX", "AIR", "AIR.NZ"),
        ("ASX", "BHP", "BHP.AX"),
        ("NASDAQ", "MSFT", "MSFT"),
        ("NYSE", "BRK.B", "BRK-B"),
    ],
)
def test_yahoo_symbol_mapping(exchange, code, expected):
    assert yahoo_symbol(exchange, code) == expected


def test_manual_symbol_override_always_wins():
    assert yahoo_symbol("NZX", "AIRRG", "AIRRG.NZ") == "AIRRG.NZ"
    assert YahooSource(None).symbol(Listing("UNKNOWN", "OLD", "RENAMED.AX")) == "RENAMED.AX"


def test_unknown_exchange_needs_an_override():
    with pytest.raises(UnknownSymbol, match="prices symbol"):
        yahoo_symbol("LSE", "VOD")


def test_typed_symbol_is_cleaned_and_checked():
    assert clean_symbol(" brk-b ") == "BRK-B"
    with pytest.raises(ValueError):
        clean_symbol("bad symbol")


def test_json_fetcher_retries_transient_failures_and_parses_decimal():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, text='{"close": 2.94}', request=request)

    waits = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = JsonFetcher(client, attempts=3, backoff=1, sleep=waits.append).get(
            "https://example.test/prices"
        )
    assert result == {"close": Decimal("2.94")}
    assert waits == [1, 2]


def test_json_fetcher_stops_after_its_attempt_limit():
    def handler(request):
        return httpx.Response(429, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceError, match="gave up after 2 attempts"),
    ):
        JsonFetcher(client, attempts=2, backoff=0, sleep=lambda _: None).get(
            "https://example.test/prices"
        )


def test_json_fetcher_refuses_redirects_to_other_services():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://db:5432/"}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceError, match="redirected.*refusing to follow"),
    ):
        JsonFetcher(client).get("https://example.test/prices")


def test_json_fetcher_limits_response_size_before_decoding():
    def handler(request):
        return httpx.Response(200, content=b"12345", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceError, match="response-size limit"),
    ):
        JsonFetcher(client, max_response_bytes=4).get("https://example.test/prices")


class StubFetcher:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self.value


def test_frankfurter_rates_follow_the_database_conversion_convention():
    fetcher = StubFetcher({"rates": {"2026-10-01": {"NZD": Decimal("1.723456789")}}})
    rates = FrankfurterSource(fetcher).rates("USD", "NZD", date(2026, 10, 1), date(2026, 10, 2))
    assert rates == {date(2026, 10, 1): Decimal("1.723456789")}
    assert fetcher.calls[0][1] == {"base": "USD", "symbols": "NZD"}


def test_yahoo_history_uses_market_dates_and_keeps_exact_decimals():
    stamp = int(datetime(2026, 10, 1, 4, tzinfo=UTC).timestamp())
    fetcher = StubFetcher(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"exchangeTimezoneName": "Pacific/Auckland"},
                        "timestamp": [stamp],
                        "indicators": {"quote": [{"close": [Decimal("2.940000057220459")]}]},
                        "events": {
                            "splits": {
                                str(stamp): {
                                    "date": stamp,
                                    "numerator": 2,
                                    "denominator": 1,
                                }
                            }
                        },
                    }
                ]
            }
        }
    )
    history = YahooSource(fetcher).history("AIR.NZ", date(2026, 10, 1), date(2026, 10, 1))
    assert history.closes == {date(2026, 10, 1): Decimal("2.940000")}
    assert history.splits == {date(2026, 10, 1): (Decimal("1"), Decimal("2"))}
    assert fetcher.calls[0][0].endswith("/AIR.NZ")


def test_yahoo_refuses_zero_exchange_rates_so_a_fallback_can_run():
    stamp = int(datetime(2026, 10, 1, 17, tzinfo=UTC).timestamp())
    fetcher = StubFetcher(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"exchangeTimezoneName": "UTC", "currency": "NZD"},
                        "timestamp": [stamp],
                        "indicators": {"quote": [{"close": [0]}]},
                    }
                ]
            }
        }
    )
    with pytest.raises(SourceError, match="impossible USD/NZD rate"):
        YahooSource(fetcher).rates("USD", "NZD", date(2026, 10, 1), date(2026, 10, 1))


def test_json_fetcher_wraps_excessive_json_nesting(monkeypatch):
    import sys

    from portfolio_digest.prices import http

    def recursion_error(*args, **kwargs):
        raise RecursionError("JSON nesting exceeded the parser limit")

    # Parser depth limits vary between Python versions. Simulate the decoder's
    # failure so this regression still exercises the error path on all versions.
    monkeypatch.setattr(http.json, "loads", recursion_error)
    depth = sys.getrecursionlimit() * 2
    body = b"[" * depth + b"0" + b"]" * depth
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        ) as client,
        pytest.raises(SourceError, match="not JSON"),
    ):
        JsonFetcher(client).get("https://example.test/prices")


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"chart": {"result": [None]}},
        {"chart": {"result": [{"meta": None}]}},
        {"chart": []},
        {"chart": {"result": [{"meta": []}]}},
    ],
)
def test_yahoo_wraps_malformed_chart_shapes(data):
    with pytest.raises(SourceError, match="Yahoo sent"):
        YahooSource(StubFetcher(data)).history("TEST", date(2026, 10, 1), date(2026, 10, 2))


def _chart(closes, *, currency="USD", splits=()):
    def stamp(day):
        return int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())

    return {
        "chart": {
            "result": [
                {
                    "meta": {"exchangeTimezoneName": "UTC", "currency": currency},
                    "timestamp": [stamp(day) for day in closes],
                    "indicators": {"quote": [{"close": list(closes.values())}]},
                    "events": {
                        "splits": {
                            str(stamp(day)): {
                                "date": stamp(day),
                                "denominator": before,
                                "numerator": after,
                            }
                            for day, before, after in splits
                        }
                    },
                }
            ]
        }
    }


def test_yahoo_restores_raw_closes_before_splits_including_later_events():
    first, split_day, later = date(2026, 9, 1), date(2026, 10, 2), date(2026, 10, 5)
    fetcher = StubFetcher(
        _chart(
            {first: Decimal("20"), split_day: Decimal("20"), later: Decimal("40")},
            splits=[(split_day, 1, 4), (later, 2, 1)],
        )
    )
    source = YahooSource(fetcher)
    history = source.history("TEST", first, split_day, currency="USD", split_start=first)
    # Undo the 4:1 split and the later 1:2 reverse split. The split day's close
    # already uses that day's new units, so only the later split affects it.
    assert history.closes == {first: Decimal("40"), split_day: Decimal("10")}
    assert history.splits == {
        split_day: (Decimal("1"), Decimal("4")),
        later: (Decimal("2"), Decimal("1")),
    }
    source.history("TEST", split_day, split_day, currency="USD", split_start=first)
    params = fetcher.calls[-1][1]
    assert params["period1"] < int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())
    assert params["period2"] > int(datetime(2026, 10, 5, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("quoted", "currency", "expected"),
    [
        ("GBp", "GBP", Decimal("1.2345")),
        ("ZAc", "ZAR", Decimal("1.2345")),
        ("ILA", "ILS", Decimal("1.2345")),
        ("GBP", "GBP", Decimal("123.45")),
    ],
)
def test_yahoo_converts_minor_units_to_instrument_currency(quoted, currency, expected):
    day = date(2026, 10, 1)
    source = YahooSource(StubFetcher(_chart({day: Decimal("123.45")}, currency=quoted)))
    assert source.history("OVERRIDE", day, day, currency=currency).closes == {day: expected}


@pytest.mark.parametrize("quoted", ["USD", "GBp", None])
def test_yahoo_rejects_mismatched_or_missing_quote_currency(quoted):
    day = date(2026, 10, 1)
    source = YahooSource(StubFetcher(_chart({day: Decimal("100")}, currency=quoted)))
    with pytest.raises(SourceError, match="currency does not match NZD"):
        source.history("OVERRIDE", day, day, currency="NZD")


def test_malformed_yahoo_fx_can_fall_back_to_frankfurter():
    day = date(2026, 10, 1)
    for source in [
        YahooSource(StubFetcher({"chart": {"result": [None]}})),
        FrankfurterSource(StubFetcher({"rates": {day.isoformat(): {"NZD": Decimal("1.7")}}})),
    ]:
        try:
            rates = source.rates("USD", "NZD", day, day)
        except SourceError:
            continue
        break
    assert rates == {day: Decimal("1.7")}


def test_yahoo_rounds_only_after_restoring_raw_price_precision():
    first, split_day = date(2026, 10, 1), date(2026, 10, 2)
    source = YahooSource(
        StubFetcher(
            _chart(
                {first: Decimal("13.333333015441895"), split_day: Decimal("13.333333015441895")},
                splits=[(split_day, 1, 3)],
            )
        )
    )
    history = source.history("TEST", first, split_day, currency="USD")
    assert history.closes == {first: Decimal("40.00000"), split_day: Decimal("13.33333")}


@pytest.mark.parametrize("close", ["1e30", "1e14", "99999999999999.999999"])
def test_yahoo_rejects_prices_outside_database_precision(close):
    day = date(2026, 10, 1)
    source = YahooSource(StubFetcher(_chart({day: Decimal(close)})))
    with pytest.raises(SourceError, match="out-of-range price"):
        source.history("TEST", day, day, currency="USD")


@pytest.mark.parametrize("rate", ["1e-11", "0.000000000049", "1e10", "1e30"])
@pytest.mark.parametrize("provider", ["yahoo", "frankfurter"])
def test_fx_sources_reject_rates_outside_database_precision(provider, rate):
    day = date(2026, 10, 1)
    if provider == "yahoo":
        source = YahooSource(StubFetcher(_chart({day: Decimal(rate)}, currency="NZD")))
    else:
        source = FrankfurterSource(
            StubFetcher({"rates": {day.isoformat(): {"NZD": Decimal(rate)}}})
        )
    with pytest.raises(SourceError):
        source.rates("USD", "NZD", day, day)


def test_frankfurter_rejects_rate_that_rounds_up_past_database_limit():
    day = date(2026, 10, 1)
    source = FrankfurterSource(
        StubFetcher({"rates": {day.isoformat(): {"NZD": Decimal("9999999999.99999999995")}}})
    )
    with pytest.raises(SourceError, match="impossible USD/NZD rate"):
        source.rates("USD", "NZD", day, day)


@pytest.mark.parametrize("provider", ["yahoo", "frankfurter"])
def test_fx_sources_accept_smallest_rate_that_rounds_positive(provider):
    day = date(2026, 10, 1)
    rate = Decimal("0.00000000005")
    if provider == "yahoo":
        source = YahooSource(StubFetcher(_chart({day: rate}, currency="NZD")))
    else:
        source = FrankfurterSource(StubFetcher({"rates": {day.isoformat(): {"NZD": rate}}}))
    assert source.rates("USD", "NZD", day, day) == {day: rate}
