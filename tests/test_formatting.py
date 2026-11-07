"""Readable Decimal display without changing the caller's arithmetic context."""

from decimal import Decimal, getcontext

from portfolio_digest.formatting import display_number, display_price, display_quantity


def test_rounded_zero_drops_the_minus_sign():
    assert display_number(Decimal("-0.004")) == "0.00"
    assert display_number(Decimal("-0.001")) == "0.00"
    assert display_number(Decimal("0.004")) == "0.00"
    assert display_number(Decimal("-0.00")) == "0.00"
    assert display_number(Decimal("-0.005")) == "-0.01"
    assert display_number(Decimal("0.005")) == "0.01"
    assert display_number(Decimal("1.225")) == "1.23"
    assert display_number(Decimal("-1.225")) == "-1.23"
    assert display_number(Decimal("1.224")) == "1.22"
    assert display_number(Decimal("1234.5")) == "1,234.50"
    assert display_number(Decimal("1234567890123.456")) == "1,234,567,890,123.46"


def test_quantity_and_price_are_trimmed_fixed_point():
    assert display_quantity(Decimal("0E-8")) == "0"
    assert display_quantity(Decimal("-0E-8")) == "0"
    assert display_quantity(Decimal("-0.000000001")) == "0"
    assert display_quantity(Decimal("1.23000000")) == "1.23"
    assert display_quantity(Decimal("-100.50000000")) == "-100.5"
    assert display_quantity(Decimal("1.5E-7")) == "0.00000015"
    assert display_quantity(Decimal("2.50E-77")) == "0"
    assert display_quantity(Decimal("100")) == "100"
    # Half even at the eighth place: 8 stays even, 7 rounds up to 8.
    assert display_quantity(Decimal("1.123456785")) == "1.12345678"
    assert display_quantity(Decimal("1.123456775")) == "1.12345678"
    assert display_quantity(Decimal("1.123456786")) == "1.12345679"
    assert display_quantity(Decimal("1.000000005")) == "1"
    assert display_quantity(Decimal("1.000000015")) == "1.00000002"
    assert display_quantity(Decimal("-0.000000006")) == "-0.00000001"
    assert display_quantity(Decimal("1E+30")) == "1" + "0" * 30
    assert "e" not in display_quantity(Decimal("1E+30")).lower()

    assert display_price(Decimal("8.50")) == "8.5"
    assert display_price(Decimal("12.3400")) == "12.34"
    assert display_price(Decimal("0.0012")) == "0.0012"
    assert display_price(Decimal("0.00125")) == "0.0013"
    assert display_price(Decimal("0.00124")) == "0.0012"
    assert display_price(Decimal("-0.00004")) == "0"
    assert display_price(Decimal("-0.00005")) == "-0.0001"
    assert display_price(Decimal("1.5E-7")) == "0"
    assert display_price(Decimal("1E+20")) == "1" + "0" * 20
    assert "e" not in display_price(Decimal("1.5E+20")).lower()


def test_formatters_leave_low_and_high_caller_precision_unchanged():
    original = getcontext().prec
    try:
        for precision in (8, 100):
            getcontext().prec = precision
            assert display_quantity(Decimal("123456789012345.123456789")) == (
                "123456789012345.12345679"
            )
            assert display_price(Decimal("0.00125")) == "0.0013"
            assert display_number(Decimal("-0.004")) == "0.00"
            assert display_number(Decimal("1234567890123.456")) == "1,234,567,890,123.46"
            assert getcontext().prec == precision
    finally:
        getcontext().prec = original
