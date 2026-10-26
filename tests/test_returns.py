"""All figures and securities are invented; expected answers are hand-worked."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from decimal import getcontext, localcontext

import pytest

from portfolio_digest.returns import (
    Components,
    Holding,
    Observation,
    Payment,
    ReturnError,
    SplitEvent,
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
    with pytest.raises(ReturnError, match="Unknown"):
        trade(kind="transfer")
    with pytest.raises(ReturnError, match="non-zero"):
        trade(kind="sell", quantity="-1")
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


@pytest.mark.parametrize("sale", ["0", "2", "7"])
@pytest.mark.parametrize("price", ["0", "7.25", "15"])
@pytest.mark.parametrize("rate", ["1.2", "2"])
@pytest.mark.parametrize("adjust", ["none", "in", "out", "split"])
def test_component_and_cashflow_identities(sale, price, rate, adjust):
    extra = {
        "none": [],
        "in": [trade(MID, "adjustment", "3", "10.5", fx="1.55")],
        "out": [trade(MID, "adjustment", "-3", "10.5", fx="1.55")],
        "split": [trade(date(2026, 10, 14), "adjustment", "20")],
    }[adjust]
    h = holding(
        "USD",
        [trade(), *extra] + ([trade(MID, "sell", sale, "11", "1", "1.7")] if sale != "0" else []),
        p1=price,
    )
    h = replace(h, splits=(SplitEvent(date(2026, 10, 14), D(1), D(3)),))
    rates = {"USD": (Observation(START, D("1.5")), Observation(END, D(rate)))}
    report = calculate_returns((h, replace(h, exchange="ASX", code="OTHER")), START, END, rates)
    for r in (*report.holdings, *report.markets, report.total):
        assert r.amounts.capital + r.amounts.dividends + r.amounts.currency == r.amounts.total
        assert (
            r.amounts.total
            == r.closing + r.sales - r.opening - r.purchases - r.adjustments + r.amounts.dividends
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


def test_table_keeps_holdings_when_exchange_contains_colon():
    from portfolio_digest.returns_table import render_table

    report = calculate_returns((holding(), replace(holding(), exchange="NZX:ALT")), START, END, {})
    table = render_table(report)
    assert table.count("NZX:FICTION") == 1
    assert table.count("NZX:ALT:FICTION") == 1
    assert report.total.closing == 240


def test_schema_sized_values_preserve_gain_and_render_without_changing_context():
    from portfolio_digest.returns_table import render_table

    quantity = D("9999999999999999.99999999")
    h = holding(
        trades=[trade(quantity=str(quantity))],
        p0="99999999999999.000000",
        p1="99999999999999.000001",
    )
    precision = getcontext().prec
    report = calculate_returns((h,), START, END, {})
    assert report.total.amounts.capital == D("9999999999.99999999999999")
    with localcontext() as context:
        context.prec = 80
        assert report.total.amounts.total == report.total.closing - report.total.opening
    assert "10,000,000,000.00" in render_table(report)
    assert getcontext().prec == precision


SPLIT_DAY = date(2026, 10, 14)


def test_adjustment_before_period_counts_as_opening_quantity():
    h = holding(trades=[trade(), trade(date(2026, 5, 1), "adjustment", "-4", "0")])
    r = holding_return(h, START, END, {})
    assert (r.opening, r.closing, r.adjustments) == (D(60), D(72), D(0))
    assert r.amounts.capital == 12 and r.denominator == 60


def test_split_adjustment_rescales_parcels_without_gain():
    # 10 units at 10 split 3-for-1 into 30 units. The closing price is a raw 4.
    h = Holding(
        "NZX",
        "FICTION",
        "NZD",
        (trade(), trade(SPLIT_DAY, "adjustment", "20"), trade(MID, "sell", "6", "4")),
        (Observation(START, D(10)), Observation(END, D(4))),
        splits=(SplitEvent(SPLIT_DAY, D(1), D(3)),),
    )
    r = holding_return(h, START, END, {})
    # Cost per new unit is 10/3; 30 units at 4 gain 20 in total.
    assert r.opening == 100 and r.closing == 96 and r.sales == 24
    assert r.adjustments == 0 and r.purchases == 0
    assert r.amounts.capital.quantize(D(".01")) == D(20)
    assert r.amounts.total.quantize(D(".01")) == D(20)


def test_exact_consolidation_and_fractional_non_split_removal():
    consolidation = SplitEvent(SPLIT_DAY, D(10), D(1))
    h = Holding(
        "NZX",
        "FICTION",
        "NZD",
        (trade(quantity="25"), trade(SPLIT_DAY, "adjustment", "-22.5")),
        (Observation(START, D(4)), Observation(END, D(45))),
        splits=(consolidation,),
    )
    r = holding_return(h, START, END, {})
    assert r.adjustments == 0 and r.closing == D("112.5")
    assert r.amounts.capital == D("12.5")
    # Removing the entire half-share is not an exact consolidation. Its cost
    # must be realised rather than disappearing into zero-unit parcels.
    rounded = replace(h, trades=(trade(quantity=".5"), trade(SPLIT_DAY, "adjustment", "-.5", "0")))
    r = holding_return(rounded, START, END, {})
    assert r.opening == 2 and r.closing == 0 and r.amounts.capital == -2
    assert r.amounts.total == r.closing - r.opening - r.adjustments


def test_zero_fee_foreign_split_does_not_need_split_date_fx():
    split_day = date(2026, 10, 16)
    h = replace(
        holding(
            "USD", trades=[trade(MID, fx="1.5"), trade(split_day, "adjustment", "10", "0")], p1="6"
        ),
        splits=(SplitEvent(split_day, D(1), D(2)),),
    )
    # Only closing FX is available after the explicitly converted purchase.
    r = holding_return(h, START, END, {"USD": (Observation(END, D("1.6")),)})
    assert r.amounts == Components(D(30), D(0), D(12))


@pytest.mark.parametrize("fees", ["0", "2"])
def test_foreign_split_preserves_each_parcels_cost_and_fx_on_fifo_sale(fees):
    split_day = date(2026, 10, 16)
    h = replace(
        holding(
            "USD",
            trades=[
                trade(MID, quantity="10", price="10", fx="1.5"),
                trade(MID, quantity="5", price="20", fx="1.6"),
                trade(split_day, "adjustment", "30", "0", fees, "1.55"),
                trade(date(2026, 10, 17), "sell", "30", "7", fx="1.65"),
            ],
            p1="8",
        ),
        splits=(SplitEvent(split_day, D(1), D(3)),),
    )
    r = holding_return(h, START, END, {"USD": (Observation(END, D("1.7")),)})
    # The first parcel's 30 split units sell for 210 local, against cost 100:
    # capital 110 x 1.5 = 165; currency 210 x .15 = 31.5.
    # The second parcel's 15 units close at 120 local, against cost 100:
    # capital 20 x 1.6 = 32; currency 120 x .1 = 12.
    fee = D(fees) * D("1.55")
    assert r.amounts == Components(D(197) - fee, D(0), D("43.5"))
    assert r.purchases == 310 + fee and r.sales == D("346.5")
    assert r.closing == 204 and r.adjustments == 0
    assert r.amounts.total == r.closing + r.sales - r.purchases


def test_split_day_only_prioritises_one_matching_adjustment():
    h = replace(
        holding(trades=[trade(MID, "adjustment", "-5", "10"), trade(MID)]),
        splits=(SplitEvent(MID, D(1), D(2)),),
    )
    r = holding_return(h, START, END, {})
    assert r.closing == 60 and r.adjustments == -50 and r.amounts.total == 10
    # Non-matching additions must not make a later removal look like a split.
    h = replace(h, splits=(SplitEvent(MID, D(2), D(1)),))
    assert holding_return(h, START, END, {}).adjustments == -50
    # The actual split comes before another adjustment regardless of input order.
    h = replace(
        holding(
            trades=[
                trade(),
                trade(MID, "adjustment", "-2", "6"),
                trade(MID, "adjustment", "10", "0"),
                trade(MID, quantity="1", price="6"),
            ],
            p1="6",
        ),
        splits=(SplitEvent(MID, D(1), D(2)),),
    )
    r = holding_return(h, START, END, {})
    assert r.closing == 114 and r.adjustments == -12 and r.amounts.capital == 20


def test_non_split_adjustments_move_units_at_recorded_price_without_cash():
    drp = trade(MID, "adjustment", "2", "11")
    transfer_out = trade(date(2026, 10, 16), "adjustment", "-5", "11.5")
    gift_at_zero = trade(date(2026, 10, 16), "adjustment", "1", "0")
    h = holding(trades=[trade(), drp, transfer_out, gift_at_zero])
    r = holding_return(h, START, END, {})
    # Transfer out takes 5 opening units: gain 7.50. Left: 5 opening (+10), 2 DRP (+2),
    # 1 gifted at 0 (+12). Closing 8 units at 12 = 96.
    assert r.purchases == 0 and r.sales == 0
    assert r.adjustments == D(22) - D("57.5")
    assert r.closing == 96
    assert r.amounts.capital == D("31.5")
    assert r.denominator == 100
    assert r.amounts.total == r.closing - r.opening - r.adjustments
    with pytest.raises(ReturnError, match="exceeds"):
        holding_return(
            holding(trades=[trade(), trade(MID, "adjustment", "-11", "10")]), START, END, {}
        )


def test_adjustment_on_split_day_that_does_not_match_is_non_cash():
    h = replace(
        holding(trades=[trade(), trade(SPLIT_DAY, "adjustment", "3", "10")]),
        splits=(SplitEvent(SPLIT_DAY, D(1), D(2)),),
    )
    r = holding_return(h, START, END, {})
    assert r.adjustments == 30 and r.closing == 156


def test_same_day_trades_apply_additions_before_removals():
    # Input order has the sale first; it must not look like an oversell.
    h = holding(trades=[trade(MID, "sell", "10", "11"), trade(MID, "buy", "10", "10.5")])
    r = holding_return(h, START, END, {})
    assert r.closing == 0 and r.amounts.capital == 5
    # Day trading before the period does not trip the opening quantity check either.
    h = holding(trades=[trade(OLD, "sell", "10", "11"), trade(OLD, "buy", "10", "10.5")])
    assert calculate_returns((h,), START, END, {}).holdings == ()


def test_weekends_and_market_holidays_are_not_flagged_as_carried_forward():
    friday, sunday = date(2026, 10, 16), date(2026, 10, 18)
    labour_day, tuesday = date(2026, 10, 26), date(2026, 10, 27)
    # NZX is shut on Labour Day, so Friday's close is the right price.
    h = replace(
        holding(), prices=(Observation(START, D(10)), Observation(date(2026, 10, 23), D(12)))
    )
    assert holding_return(h, START, labour_day, {}).flags == ()
    usd = replace(h, currency="USD")
    rates = {"USD": (Observation(START, D("1.5")), Observation(date(2026, 10, 23), D("1.6")))}
    # Currencies still trade on an NZ holiday, so the FX rate is carried forward.
    assert [f for f in holding_return(usd, START, labour_day, rates).flags if "FX" in f]
    weekend = replace(h, prices=(Observation(START, D(10)), Observation(friday, D(12))))
    weekend_rates = {"USD": (Observation(START, D("1.5")), Observation(friday, D("1.6")))}
    assert holding_return(weekend, START, sunday, weekend_rates).flags == ()
    # Tuesday after Labour Day is a trading day, so Friday's close is carried forward.
    flags = holding_return(usd, START, tuesday, rates).flags
    assert any("FICTION price" in f for f in flags) and any("USD/NZD FX" in f for f in flags)
    # A row the price service stored as stale is always flagged.
    stale = replace(weekend, prices=(Observation(START, D(10)), Observation(friday, D(12), True)))
    assert holding_return(stale, START, sunday, weekend_rates).flags


def test_holdings_sold_before_the_period_are_left_out():
    gone = replace(holding(trades=[trade(), trade(date(2026, 6, 1), "sell")]), code="GONE")
    sold_now = replace(holding(trades=[trade(), trade(MID, "sell")]), code="SOLDNOW")
    report = calculate_returns((gone, sold_now, holding()), START, END, {})
    assert [r.label for r in report.holdings] == ["NZX:FICTION", "NZX:SOLDNOW"]
    paid_after_sale = replace(gone, income=(Payment(MID, D(1), "NZD"),))
    report = calculate_returns((paid_after_sale,), START, END, {})
    assert report.total.amounts.dividends == 1
