"""Stock exchanges we know about, and the currency each one trades in.

Brokers name the same exchange in different ways ("NZSE", "NZX", "nzx"). Every importer
goes through `exchange_code()` so an instrument is stored once, under one name, whichever
file it came from.
"""

from portfolio_digest.validation import InvalidInput

# Canonical exchange code -> the currency its listings trade in.
EXCHANGE_CURRENCY = {
    "NZX": "NZD",
    "ASX": "AUD",
    "NYSE": "USD",
    "NASDAQ": "USD",
    "NYSEARCA": "USD",
    "NYSEAMERICAN": "USD",
    "CBOE": "USD",
}

# Other spellings brokers use, after upper-casing and removing spaces.
_ALIASES = {
    "NZSE": "NZX",
    "NZ": "NZX",
    "AX": "ASX",
    "AU": "ASX",
    "ARCA": "NYSEARCA",
    "NYSEMKT": "NYSEAMERICAN",
    "AMEX": "NYSEAMERICAN",
    "BATS": "CBOE",
    "CBOEBZX": "CBOE",
    "BZX": "CBOE",
}


def exchange_code(value: str) -> str:
    """The canonical code for an exchange, e.g. "nzse" -> "NZX"."""
    key = "".join(value.split()).upper()
    key = _ALIASES.get(key, key)
    if key not in EXCHANGE_CURRENCY:
        known = ", ".join(EXCHANGE_CURRENCY)
        raise InvalidInput(f"{value!r} is not an exchange we know. Known exchanges: {known}.")
    return key


def exchange_currency(exchange: str) -> str:
    return EXCHANGE_CURRENCY[exchange_code(exchange)]


def instrument_code(value: str) -> str:
    """A listing code as the exchange writes it: upper case, no spaces."""
    code = value.strip().upper()
    if not code or any(ch.isspace() for ch in code):
        raise InvalidInput(f"{value!r} is not a share code such as AIR or FPH.")
    if len(code) > 32:
        raise InvalidInput(f"Share code {code!r} is longer than 32 characters.")
    return code
