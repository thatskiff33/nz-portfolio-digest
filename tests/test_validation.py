import pytest

from portfolio_digest.validation import (
    PORTFOLIO_NAME_MAX,
    InvalidInput,
    currency_code,
    portfolio_name,
)


@pytest.mark.parametrize(("raw", "expected"), [("NZD", "NZD"), (" aud ", "AUD"), ("usd", "USD")])
def test_currency_code_is_normalised(raw, expected):
    assert currency_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "NZ", "NZDX", "N1D", "NZ D"])
def test_currency_code_rejects_non_iso_values(raw):
    with pytest.raises(InvalidInput):
        currency_code(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Family Trust ", "Family Trust"),
        ("Family   Trust", "Family Trust"),
        ("Family\tTrust\n", "Family Trust"),
    ],
)
def test_portfolio_name_whitespace_is_tidied(raw, expected):
    assert portfolio_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "Bad\x00name", "x" * (PORTFOLIO_NAME_MAX + 1)])
def test_portfolio_name_rejects_unusable_names(raw):
    with pytest.raises(InvalidInput):
        portfolio_name(raw)


def test_portfolio_name_allows_the_maximum_length():
    assert portfolio_name("x" * PORTFOLIO_NAME_MAX) == "x" * PORTFOLIO_NAME_MAX
