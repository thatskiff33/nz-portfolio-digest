"""Checks and clean-ups for values typed in by a person, shared by the CLI and settings.

The database has matching CHECK constraints, so bad data is stopped either way. Checking
here first means people get a clear message instead of a database error.
"""

import re
import unicodedata

PORTFOLIO_NAME_MAX = 120

_CURRENCY = re.compile(r"[A-Z]{3}")


class InvalidInput(ValueError):
    """A value a person supplied is not acceptable. The message says why, in plain English."""


def currency_code(value: str) -> str:
    """An ISO 4217 code such as NZD. Accepts lower case and surrounding spaces."""
    code = value.strip().upper()
    if not _CURRENCY.fullmatch(code):
        raise InvalidInput(f"{value!r} is not a three-letter currency code such as NZD.")
    return code


def portfolio_name(value: str) -> str:
    """Trims the name and squashes runs of spaces, tabs or newlines into a single space."""
    name = " ".join(value.split())
    if not name:
        raise InvalidInput("Portfolio name cannot be blank.")
    # Control characters (Cc) and lone surrogates (Cs, from bytes that are not valid
    # UTF-8). Invisible joiners such as the one inside 👨‍👩‍👧 are fine.
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in name):
        raise InvalidInput("Portfolio name cannot contain control characters or invalid text.")
    if len(name) > PORTFOLIO_NAME_MAX:
        raise InvalidInput(f"Portfolio name must be {PORTFOLIO_NAME_MAX} characters or fewer.")
    return name
