"""Turn an exchange and code into the symbol Yahoo Finance uses.

NZX codes get `.NZ` and ASX codes `.AX`; US codes are used as they are, except that Yahoo
writes share classes with a dash (BRK.B is BRK-B). Anything else, such as rights (AIRRG),
or delisted and renamed codes, is set by hand on the instrument with
`portfolio-digest prices symbol EXCHANGE:CODE SYMBOL`, which always wins.
"""

import re

from portfolio_digest.validation import InvalidInput

YAHOO_SUFFIXES = {"NZX": ".NZ", "ASX": ".AX"}
US_EXCHANGES = frozenset({"NYSE", "NASDAQ"})

_SYMBOL = re.compile(r"[A-Z0-9.\-=^]{1,32}")


class UnknownSymbol(ValueError):
    """There is no automatic Yahoo symbol for this exchange; one must be set by hand."""


def yahoo_symbol(exchange: str, code: str, override: str | None = None) -> str:
    if override:
        return override
    if exchange in YAHOO_SUFFIXES:
        return f"{code}{YAHOO_SUFFIXES[exchange]}"
    if exchange in US_EXCHANGES:
        return code.replace(".", "-")
    raise UnknownSymbol(
        f"No automatic Yahoo symbol for {exchange}:{code}. "
        f"Set one with `portfolio-digest prices symbol {exchange}:{code} SYMBOL`."
    )


def clean_symbol(value: str) -> str:
    """A symbol typed in by a person, e.g. `airrg.nz`. Raises InvalidInput when unusable."""
    symbol = value.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise InvalidInput(
            f"{value!r} is not a usable symbol: up to 32 letters, digits and . - = ^ only."
        )
    return symbol
