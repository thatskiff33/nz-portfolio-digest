from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_digest.importers.asb import AsbEmailImporter, AsbStatementImporter
from portfolio_digest.importers.base import FileProblem, ParseContext
from portfolio_digest.importers.sharesies import SharesiesImporter
from portfolio_digest.importers.sharesight_csv import SharesightCsvImporter
from portfolio_digest.models import TradeType
from portfolio_digest.reconcile import compare, parse_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_sharesies_reads_all_markets_smart_etf_and_fractional_units():
    result = SharesiesImporter().parse(fixture("sharesies-transactions.csv"), ParseContext())

    assert result.errors == []
    assert len(result.trades) == 5
    assert len(result.skipped) == 1
    assert [(trade.exchange, trade.code, trade.currency) for trade in result.trades] == [
        ("NZX", "AIR", "NZD"),
        ("NZX", "USF", "NZD"),
        ("ASX", "BHP", "AUD"),
        ("NASDAQ", "MSFT", "USD"),
        ("NZX", "AIR", "NZD"),
    ]
    assert result.trades[0].quantity == Decimal("12.34567890")
    assert result.trades[3].price == Decimal("410.123457")
    assert result.trades[3].fx_rate == Decimal("1.6393442623")


def test_sharesies_reports_bad_rows_without_losing_good_ones():
    content = fixture("sharesies-transactions.csv") + (
        b"bad,31/02/2024,AIR,NZX,one,7,BUY,,0,NZD,7,Order\n"
    )
    result = SharesiesImporter().parse(content, ParseContext())

    assert len(result.trades) == 5
    assert len(result.errors) == 1
    assert "line 8" in str(result.errors[0])


def test_sharesies_refuses_non_nzd_portfolio_because_its_fx_rate_is_nzd_based():
    with pytest.raises(FileProblem, match="only be imported into an NZD portfolio"):
        SharesiesImporter().parse(fixture("sharesies-transactions.csv"), ParseContext("AUD"))


def test_sharesies_rejects_currency_that_disagrees_with_exchange():
    content = (
        fixture("sharesies-transactions.csv")
        .replace(b",BHP,ASX,", b",BHP,ASX,")
        .replace(b",AUD,115.00", b",USD,115.00")
    )
    result = SharesiesImporter().parse(content, ParseContext())
    assert "trades in AUD, but the row says USD" in str(result.errors[0])


def test_sharesight_format_maps_exchange_rate_and_base_currency_fee():
    result = SharesightCsvImporter().parse(fixture("sharesight-trades.csv"), ParseContext("NZD"))

    assert result.errors == []
    assert len(result.trades) == 3
    first = result.trades[0]
    assert (first.exchange, first.currency) == ("NYSEARCA", "USD")
    assert first.fx_rate == Decimal("1.6326530612")
    assert first.fees == Decimal("3.062500")
    assert result.trades[1].exchange == "NZX"


def test_sharesight_fingerprint_is_stable_across_number_formatting():
    importer = SharesightCsvImporter()
    first = importer.parse(fixture("sharesight-trades.csv"), ParseContext()).trades[1]
    reformatted = fixture("sharesight-trades.csv").replace(b",10,29.25,", b",10.0,29.2500,")
    second = importer.parse(reformatted, ParseContext()).trades[1]
    assert first.external_ref == second.external_ref


def test_sharesight_reports_a_malformed_row():
    content = fixture("sharesight-trades.csv") + (
        b"not-a-date,AIR,NZX,1,7,BUY,,0,NZD,Broken made-up row\n"
    )
    result = SharesightCsvImporter().parse(content, ParseContext())
    assert len(result.trades) == 3
    assert "not a date" in str(result.errors[0])


def test_asb_contract_note_extracts_a_trade_and_exact_fees():
    result = AsbEmailImporter().parse(fixture("asb-contract-note.eml"), ParseContext())
    trade = result.trades[0]

    assert result.errors == []
    assert trade.external_ref == "contract:40012345"
    assert trade.trade_type == TradeType.BUY
    assert trade.fees == Decimal("34.50")
    assert trade.name == "Example Healthcare Limited"


def test_asb_contract_note_reports_a_missing_required_field():
    content = fixture("asb-contract-note.eml").replace(b"Quantity: 100\n", b"")
    result = AsbEmailImporter().parse(content, ParseContext())
    assert result.trades == []
    assert "missing: quantity" in str(result.errors[0])


def test_asb_statement_reads_history_and_identifies_an_unreadable_trade_row():
    content = fixture("asb-statement.txt") + b"01/01/2019 this row is damaged\n"
    result = AsbStatementImporter().parse(content, ParseContext())

    assert len(result.trades) == 3
    assert result.trades[1].currency == "AUD"
    assert len(result.errors) == 1
    assert "cannot read this trade row" in str(result.errors[0])


def test_asb_statement_refuses_a_pdf_instead_of_guessing():
    with pytest.raises(FileProblem, match="Extract its text first"):
        AsbStatementImporter().parse(b"%PDF-1.7 not really a PDF", ParseContext())


def test_reconcile_parser_and_comparison_show_both_missing_sides():
    snapshot = parse_snapshot(fixture("reconcile-match.csv"))
    actual = {
        ("NZX", "AIR"): Decimal("10.34567890"),
        ("NZX", "USF"): Decimal("3"),
        ("NZX", "FPH"): Decimal("2"),
        ("NASDAQ", "MSFT"): Decimal("0.125"),
    }
    differences = compare(actual, snapshot)

    assert [(item.exchange, item.code) for item in differences] == [
        ("ASX", "BHP"),
        ("NZX", "FPH"),
        ("NZX", "USF"),
    ]
    assert differences[-1].variance == Decimal("-1")


def test_reconcile_rejects_duplicate_and_malformed_snapshot_rows():
    bad = b"Exchange,Code,Units\nNZX,AIR,1\nNZX,AIR,2\nASX,BHP,nope\n"
    with pytest.raises(FileProblem) as exc:
        parse_snapshot(bad)
    assert "line 3" in str(exc.value)
    assert "line 4" in str(exc.value)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b",7.25,BUY", b",USD 7.25,BUY"),
        (b",1.25,NZD", b",AUD 1.25,NZD"),
    ],
)
def test_sharesies_rejects_price_and_fee_currency_prefix_mismatch(old, new):
    content = fixture("sharesies-transactions.csv").replace(old, new)
    result = SharesiesImporter().parse(content, ParseContext())
    assert len(result.errors) == 1
    assert "expected NZD" in str(result.errors[0])
    assert "line 2" in str(result.errors[0])


@pytest.mark.parametrize("field", [b"Price", b"Brokerage", b"GST", b"Consideration"])
def test_asb_rejects_currency_prefix_mismatch(field):
    content = fixture("asb-contract-note.eml").replace(field + b": NZD", field + b": AUD")
    result = AsbEmailImporter().parse(content, ParseContext())
    assert result.trades == []
    assert len(result.errors) == 1
    assert "NZD" in str(result.errors[0]) and "AUD" in str(result.errors[0])


def test_sharesight_checks_prefix_against_declared_brokerage_currency():
    content = fixture("sharesight-trades.csv").replace(b",5.00,NZD", b",USD 5.00,NZD")
    result = SharesightCsvImporter().parse(content, ParseContext())
    assert len(result.errors) == 1
    assert "Brokerage is in USD, but expected NZD" in str(result.errors[0])
    valid = fixture("sharesight-trades.csv").replace(b",5.00,NZD", b",NZD 5.00,NZD")
    result = SharesightCsvImporter().parse(valid, ParseContext())
    assert result.errors == []
    assert result.trades[0].fees == Decimal("3.062500")


def test_sharesies_accepts_matching_price_and_fee_prefixes():
    content = fixture("sharesies-transactions.csv").replace(b",7.25,BUY", b",NZD 7.25,BUY")
    content = content.replace(b",1.25,NZD", b",NZD 1.25,NZD")
    result = SharesiesImporter().parse(content, ParseContext())
    assert result.errors == []
    assert result.trades[0].price == Decimal("7.250000")
    assert result.trades[0].fees == Decimal("1.25")


@pytest.mark.parametrize("value", ["1e30", "1e14", "99999999999999.9999999", "1e999999999"])
def test_price_outside_database_range_is_a_row_problem(value):
    from portfolio_digest.importers.base import RowProblem, parse_price

    with pytest.raises(RowProblem, match="supported price range"):
        parse_price(value)


@pytest.mark.parametrize("value", ["1e-25", "1e-10", "1e30", "1e-999999999"])
def test_inverse_rate_outside_database_range_is_a_row_problem(value):
    from portfolio_digest.importers.base import RowProblem, invert_rate

    with pytest.raises(RowProblem, match="supported exchange rate range"):
        invert_rate(value)


def test_email_huge_price_is_a_row_error():
    content = fixture("asb-contract-note.eml").replace(b"27.5000", b"1e30")
    result = AsbEmailImporter().parse(content, ParseContext())
    assert not result.trades
    assert "email: Price is outside the supported price range" in str(result.errors[0])


@pytest.mark.parametrize("contract", ["9" * 120, "123/456", "ABC123", "123 456"])
@pytest.mark.parametrize("source", ["email", "statement"])
def test_asb_invalid_contract_number_is_a_located_row_error(contract, source):
    if source == "email":
        content = fixture("asb-contract-note.eml").replace(b"40012345", contract.encode())
        result = AsbEmailImporter().parse(content, ParseContext())
    else:
        content = f"14/03/2024 {contract} Buy FPH NZX 100 27.5000 34.50 2750.00\n".encode()
        result = AsbStatementImporter().parse(content, ParseContext())
    assert not result.trades
    assert len(result.errors) == 1
    assert result.errors[0].where == ("email" if source == "email" else "line 1")
    assert "contract number" in str(result.errors[0]) or "cannot read this trade row" in str(
        result.errors[0]
    )


def test_asb_contract_reference_fits_at_maximum_length():
    content = fixture("asb-contract-note.eml").replace(b"40012345", b"9" * 119)
    result = AsbEmailImporter().parse(content, ParseContext())
    assert not result.errors
    assert len(result.trades[0].external_ref) == 128


def test_broker_negative_cash_fees_still_parse_as_positive():
    from portfolio_digest.importers.base import parse_money

    assert parse_money("-5", "Brokerage") == Decimal("5")


@pytest.mark.parametrize("value", ["1e16", "1e20", "-1e16", "1e999999999"])
def test_quantity_outside_database_range_is_a_row_problem(value):
    from portfolio_digest.importers.base import RowProblem, parse_quantity

    with pytest.raises(RowProblem, match="supported quantity range"):
        parse_quantity(value, allow_negative=True)


@pytest.mark.parametrize("value", ["1e14", "1e25", "-1e14", "-1e999999999"])
def test_money_outside_database_range_is_a_row_problem(value):
    from portfolio_digest.importers.base import RowProblem, parse_money

    with pytest.raises(RowProblem, match="supported money range"):
        parse_money(value, "Brokerage")


def test_quantity_and_money_accept_database_boundaries():
    from portfolio_digest.importers.base import parse_money, parse_quantity

    quantity = "9999999999999999.99999999"
    money = "99999999999999.999999"
    assert parse_quantity(quantity) == Decimal(quantity)
    assert parse_quantity("-" + quantity, allow_negative=True) == -Decimal(quantity)
    assert parse_money(money, "Brokerage") == Decimal(money)
    assert parse_money("-" + money, "Brokerage") == Decimal(money)


@pytest.mark.parametrize("fee,rate", [("1e25", "0.6"), ("9e13", "1e9")])
def test_sharesight_oversized_brokerage_is_a_located_row_error(fee, rate):
    content = (
        "Trade Date,Instrument Code,Market Code,Quantity,Price in Dollars,"
        "Transaction Type,Exchange Rate,Brokerage,Brokerage Currency\n"
        f"03/06/2024,MSFT,NASDAQ,1,100,BUY,{rate},{fee},NZD\n"
    ).encode()
    result = SharesightCsvImporter().parse(content, ParseContext())
    assert not result.trades
    assert len(result.errors) == 1
    assert result.errors[0].where == "line 2"
    assert "Brokerage is outside the supported" in result.errors[0].message


def test_sharesight_brokerage_arithmetic_error_is_a_located_row_error():
    from decimal import localcontext

    content = (
        b"Trade Date,Instrument Code,Market Code,Quantity,Price in Dollars,"
        b"Transaction Type,Exchange Rate,Brokerage,Brokerage Currency\n"
        b"03/06/2024,MSFT,NASDAQ,1,100,BUY,1e9,9e13,NZD\n"
    )
    with localcontext() as context:
        context.Emax = 14
        result = SharesightCsvImporter().parse(content, ParseContext())
    assert not result.trades
    assert str(result.errors[0]) == "line 2: Brokerage is outside the supported money range."


@pytest.mark.parametrize(
    "gst,expected",
    [("0.999999", Decimal("99999999999999.999999")), ("1", None), ("1.000001", None)],
)
@pytest.mark.parametrize("source", ["email", "statement"])
def test_asb_combined_fees_fit_money_column(source, gst, expected):
    if source == "email":
        content = fixture("asb-contract-note.eml").replace(
            b"Brokerage: NZD 30.00\nGST: NZD 4.50",
            f"Brokerage: NZD 99999999999999\nGST: NZD {gst}".encode(),
        )
        result = AsbEmailImporter().parse(content, ParseContext())
        where = "email"
    else:
        content = f"14/03/2016 30001234 Buy FPH NZX 100 9.8500 99999999999999 {gst} 985.00".encode()
        result = AsbStatementImporter().parse(content, ParseContext())
        where = "line 1"
    if expected is None:
        assert not result.trades
        assert len(result.errors) == 1
        assert result.errors[0].where == where
        assert "Fees is outside the supported money range" in result.errors[0].message
    else:
        assert not result.errors
        assert result.trades[0].fees == expected


@pytest.mark.parametrize("length", [255, 256])
def test_asb_email_security_name_column_boundary(length):
    content = fixture("asb-contract-note.eml").replace(
        b"Security name: Example Healthcare Limited", b"Security name: " + b"A" * length
    )
    result = AsbEmailImporter().parse(content, ParseContext())
    if length == 255:
        assert not result.errors
        assert result.trades[0].name == "A" * length
    else:
        assert not result.trades
        assert result.errors[0].where == "email"
        assert "Security name must be 255 characters or fewer" in result.errors[0].message
