"""All figures and securities are invented; expected answers are hand-worked."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from portfolio_digest.returns import (
    Components,
    Holding,
    Observation,
    Payment,
    ReturnError,
    Transaction,
    calculate_returns,
    holding_return,
    period_range,
)

START = date(2026, 10, 12)
MID = date(2026, 10, 15)
END = date(2026, 10, 19)
OLD = date(2026, 1, 1)


def trade(day=OLD, kind="buy", quantity="10", price="10", fees="0", fx=None):
    return Transaction(day, kind, D(quantity), D(price), D(fees), D(fx) if fx else None)


def holding(currency="NZD", trades=None, p0="10", p1="12"):
    return Holding(
        "NZX",
        "FICTION",
        currency,
        tuple(trades or [trade()]),
        (
            Observation(START, D(p0)),
            Observation(END, D(p1)),
        ),
    )


def test_single_nzx_and_dividend():
    h = replace(holding(), income=(Payment(MID, D(5), "NZD"),))
    r = holding_return(h, START, END, {})
    assert (r.opening, r.closing, r.amounts) == (D(100), D(120), Components(D(20), D(5)))
    assert r.percentages.total == D(25)


def test_us_price_fx_cross_term_is_currency_gain():
    r = holding_return(
        holding("USD"),
        START,
        END,
        {
            "USD": (Observation(START, D("1.5")), Observation(END, D("1.6"))),
        },
    )
    # Opening 150, closing 192. Capital: 10*2*1.5=30; FX: 10*12*0.1=12.
    assert r.amounts == Components(D(30), D(0), D(12))
    assert r.percentages.total == D(28)


def test_buy_mid_period_uses_execution_price_and_fees():
    h = holding(trades=[trade(MID, price="11", fees="1")])
    r = holding_return(h, START, END, {})
    assert r.opening == 0
    assert r.purchases == 111
    assert r.amounts.capital == 9
    assert r.percentages.total.quantize(D(".01")) == D("8.11")


@pytest.mark.parametrize("quantity, closing, gain", [("4", "72", "16"), ("10", "0", "10")])
def test_partial_and_full_sales(quantity, closing, gain):
    h = holding(trades=[trade(), trade(MID, "sell", quantity, "11")])
    r = holding_return(h, START, END, {})
    assert r.closing == D(closing)
    assert r.amounts.total == D(gain)
    assert r.denominator == 100
    assert r.closing + r.sales - r.opening - r.purchases == r.amounts.total


def test_fifo_for_new_parcels_and_foreign_sale():
    h = holding(
        "USD",
        [
            trade(MID, price="10", fx="1.5"),
            trade(MID, price="20", fx="1.6"),
            trade(END, "sell", "10", "30", fx="1.7"),
        ],
        p1="30",
    )
    r = holding_return(h, START, END, {"USD": (Observation(END, D("1.7")),)})
    # First parcel sold: capital 300, FX 60. Second held: capital 160, FX 30.
    assert r.amounts == Components(D(460), D(0), D(90))
    assert r.amounts.total == r.closing + r.sales - r.purchases


def test_zero_and_stale_and_missing():
    r = holding_return(holding(p0="0", p1="0"), START, END, {})
    assert r.percentages is None
    h = replace(holding(), prices=(Observation(START, D(10)),))
    r = holding_return(h, START, END, {})
    assert r.amounts.total == 0 and r.flags
    h = replace(h, prices=(Observation(START, D(10), True), Observation(END, D(12))))
    assert holding_return(h, START, END, {}).flags
    with pytest.raises(ReturnError, match="Missing"):
        holding_return(replace(h, prices=()), START, END, {})
    with pytest.raises(ReturnError, match="Missing"):
        holding_return(holding("USD"), START, END, {})


def test_sold_holding_needs_no_closing_quote_and_income_still_counts():
    h = holding(trades=[trade(), trade(MID, "sell")])
    h = replace(h, prices=h.prices[:1], income=(Payment(END, D(2), "NZD"),))
    assert holding_return(h, START, END, {}).amounts.total == 2


def test_boundaries_and_invalid_history():
    h = replace(
        holding(trades=[trade(START), trade(END, quantity="1")]),
        income=(Payment(START, D(99), "NZD"), Payment(END, D(3), "NZD")),
    )
    r = holding_return(h, START, END, {})
    assert r.opening == 100 and r.purchases == 10 and r.amounts.dividends == 3
    with pytest.raises(ReturnError, match="exceeds"):
        holding_return(holding(trades=[trade(MID, "sell")]), START, END, {})
    with pytest.raises(ReturnError, match="Adjustments"):
        trade(kind="adjustment")
    with pytest.raises(ReturnError, match="finite"):
        Observation(START, D("NaN"))


@pytest.mark.parametrize(
    "period, expected",
    [
        ("week", date(2026, 10, 12)),
        ("month", date(2026, 9, 19)),
        ("ytd", date(2025, 12, 31)),
        ("all", date(2025, 12, 31)),
        ("2026-10-01:2026-10-19", date(2026, 10, 1)),
    ],
)
def test_periods(period, expected):
    assert period_range(period, END, OLD) == (expected, END)


def test_month_clamps_and_bad_ranges():
    assert period_range("month", date(2024, 3, 31))[0] == date(2024, 2, 29)
    for period in ("nope", "2026-10-19:2026-10-01", "2026-01-01", "x:y:z"):
        with pytest.raises(ReturnError):
            period_range(period, END)


def weekly_fixture():
    # Made-up weekly email. Each market has an opening NZ$100 holding.
    # NZX: 10 shares 10 -> 12, $3 dividend: 20+3+0=23.
    # ASX: 10 shares 8 -> 9, FX 1.25 -> 1.20: 12.50+2-4.50=10.
    # NASDAQ: 5 shares 10 -> 12, FX 2 -> 2.1: 20+0+6=26.
    # NYSE: 4 shares 12.5 -> 10, FX 2 -> 2.1: -20+0+4=-16.
    data = [
        ("NZX", "FERN", "NZD", "10", "10", "12", "3"),
        ("ASX", "WATTLE", "AUD", "10", "8", "9", "2"),
        ("NASDAQ", "ORBIT", "USD", "5", "10", "12", "0"),
        ("NYSE", "CANOPY", "USD", "4", "12.5", "10", "0"),
    ]
    holdings = tuple(
        Holding(
            m,
            c,
            cur,
            (trade(quantity=q, price=p0),),
            (Observation(START, D(p0)), Observation(END, D(p1))),
            (Payment(MID, D(div), "NZD"),),
        )
        for m, c, cur, q, p0, p1, div in data
    )
    rates = {
        "AUD": (Observation(START, D("1.25")), Observation(END, D("1.20"))),
        "USD": (Observation(START, D(2)), Observation(END, D("2.1"))),
    }
    return holdings, rates


def test_weekly_email_parity():
    holdings, rates = weekly_fixture()
    report = calculate_returns(holdings, START, END, rates)
    expected = {
        "NZX:FERN": ("20", "3", "0", "23"),
        "ASX:WATTLE": ("12.50", "2", "-4.50", "10"),
        "NASDAQ:ORBIT": ("20", "0", "6", "26"),
        "NYSE:CANOPY": ("-20", "0", "4", "-16"),
    }
    for r in report.holdings:
        for field, value in zip(
            ("capital", "dividends", "currency", "total"), expected[r.label], strict=True
        ):
            assert getattr(r.amounts, field).quantize(D(".01")) == D(value)
            assert getattr(r.percentages, field).quantize(D(".01")) == D(value)
    assert report.total.amounts == Components(D("32.50"), D(5), D("5.50"))
    assert report.total.percentages.total == D("10.75")
    assert report.total.closing == D(438)


@pytest.mark.parametrize("sale", ["0", "2", "10"])
@pytest.mark.parametrize("price", ["0", "7.25", "15"])
@pytest.mark.parametrize("rate", ["1.2", "2"])
def test_component_and_cashflow_identities(sale, price, rate):
    h = holding(
        "USD",
        [trade()] + ([trade(MID, "sell", sale, "11", "1", "1.7")] if sale != "0" else []),
        p1=price,
    )
    rates = {"USD": (Observation(START, D("1.5")), Observation(END, D(rate)))}
    report = calculate_returns((h, replace(h, exchange="ASX", code="OTHER")), START, END, rates)
    for r in (*report.holdings, *report.markets, report.total):
        assert r.amounts.capital + r.amounts.dividends + r.amounts.currency == r.amounts.total
        assert (
            r.amounts.total == r.closing + r.sales - r.opening - r.purchases + r.amounts.dividends
        )
        p = r.percentages
        assert abs(p.capital + p.dividends + p.currency - p.total) < D("1e-20")


def test_income_fx_override_and_carry_forward():
    h = replace(holding(), income=(Payment(MID, D(4), "USD", D("1.6")), Payment(END, D(2), "AUD")))
    r = holding_return(h, START, END, {"AUD": (Observation(START, D("1.25")),)})
    assert r.amounts.dividends == D("8.90")
    assert any("AUD/NZD FX" in flag for flag in r.flags)


def test_market_subtotal_weights_capital_and_income_only_zero_denominator():
    first = holding()
    second = replace(holding(p1="9", trades=[trade(quantity="30")]), code="SECOND")
    income_only = Holding("ASX", "DIVONLY", "AUD", income=(Payment(MID, D(3), "NZD"),))
    report = calculate_returns((first, second, income_only), START, END, {})
    market = next(r for r in report.markets if r.label == "NZX")
    assert market.denominator == 400
    assert market.amounts.total == -10
    assert market.percentages.total == D("-2.5")
    dividend = next(r for r in report.holdings if r.label == "ASX:DIVONLY")
    assert dividend.amounts.total == 3 and dividend.percentages is None


def test_all_includes_execution_gain_and_opening_balance():
    h = holding(trades=[trade(MID, "opening_balance", fees="2")])
    start, end = period_range("all", END, MID)
    r = holding_return(h, start, end, {})
    assert r.opening == 0 and r.purchases == 102
    assert r.amounts.capital == 18
    assert r.closing - r.purchases == r.amounts.total
