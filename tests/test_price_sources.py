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
