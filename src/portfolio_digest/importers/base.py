"""The shape every importer shares, and helpers for reading broker files.

An importer is one small module that turns a broker's file (or feed) into `ParsedTrade`s.
It never touches the database: `portfolio_digest.importing` does that, the same way for
every source. This keeps each importer easy to test on a made-up file.

Rows an importer cannot read become `RowError`s naming the line, so a person can find and
fix them. Rows that are fine but not trades (a dividend, a cash deposit) become `skipped`
notices instead, so nothing disappears without a word.
"""

import csv
import hashlib
import io
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from portfolio_digest import markets
from portfolio_digest.models import TradeType
from portfolio_digest.validation import InvalidInput

# Matches the database columns: Quantity is Numeric(24, 8), Money is Numeric(20, 6) and
# Rate is Numeric(20, 10).
QUANTITY_PLACES = 8
MONEY_PLACES = 6
RATE_PLACES = 10


class FileProblem(Exception):
    """The whole file is unusable (wrong type, missing columns). Says why, in plain English."""


class RowProblem(Exception):
    """One row cannot be read. Raised inside an importer and turned into a `RowError`."""


@dataclass(frozen=True)
class RowError:
    where: str  # "line 12", or "message 18c2f..." for emails
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass(frozen=True)
class ParsedTrade:
    where: str
    exchange: str
    code: str
    trade_date: date
    trade_type: TradeType
    quantity: Decimal
    price: Decimal
    currency: str
    external_ref: str
    fees: Decimal = Decimal(0)
    # 1 unit of `currency` is worth `fx_rate` units of `fx_base`, as the broker reported it.
    fx_rate: Decimal | None = None
    fx_base: str | None = None
    # The instrument's name when the file gives one; otherwise its code is used.
    name: str | None = None
    notes: str | None = None


@dataclass
class ParseResult:
    trades: list[ParsedTrade] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    skipped: list[RowError] = field(default_factory=list)


@dataclass(frozen=True)
class ParseContext:
    """What an importer may need to know about the portfolio it is importing into."""

    base_currency: str = "NZD"


class Importer(ABC):
    #: Name used on the command line and stored on the import run, e.g. "sharesies".
    name: str
    #: Stored on each trade. Importers that read the same broker's records share it, so a
    #: trade seen in both an email and a statement is only stored once.
    trade_source: str
    #: One line for `--help`.
    summary: str

    @abstractmethod
    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        """Read a whole file. Raises `FileProblem` when the file cannot be used at all."""


# Reading files ----------------------------------------------------------------------------


def decode_text(content: bytes) -> str:
    if content.startswith(b"%PDF"):
        raise FileProblem(
            "This is a PDF. Extract its text first, e.g. `pdftotext -layout statement.pdf "
            "statement.txt`, then import the .txt file."
        )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileProblem("This file is not UTF-8 text. Is it the right file?") from None


def _header_key(value: str) -> str:
    # "Price in Dollars (optional)" and "price  in dollars" are the same column.
    value = re.sub(r"\(.*?\)", "", value)
    return " ".join(value.lower().replace("_", " ").split())


def read_csv(
    content: bytes, columns: dict[str, tuple[str, ...]], required: set[str]
) -> Iterator[tuple[int, dict[str, str]]]:
    """Rows as (line number, {our column name: value}), recognising each header by its aliases.

    `columns` maps our name for a column to the headers it may appear under. Blank lines are
    skipped. Raises `FileProblem` when a required column is missing.
    """
    reader = csv.reader(io.StringIO(decode_text(content), newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise FileProblem("The file is empty.") from None
    except csv.Error as exc:
        raise FileProblem(f"This is not a readable CSV file ({exc}).") from None

    lookup = {_header_key(alias): ours for ours, aliases in columns.items() for alias in aliases}
    positions: dict[str, int] = {}
    for index, title in enumerate(header):
        ours = lookup.get(_header_key(title))
        if ours is not None and ours not in positions:
            positions[ours] = index
    missing = [columns[name][0] for name in columns if name in required and name not in positions]
    if missing:
        raise FileProblem(
            f"The file is missing the column(s): {', '.join(missing)}. Is it the right export?"
        )

    try:
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue
            row = {
                ours: cells[i].strip() if i < len(cells) else "" for ours, i in positions.items()
            }
            yield reader.line_num, row
    except csv.Error as exc:
        raise FileProblem(f"Line {reader.line_num} is not valid CSV ({exc}).") from None


# Reading values ---------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%d-%b-%Y")


def parse_date(value: str, what: str = "date") -> date:
    """A date written the NZ way (day first) or as YYYY-MM-DD. Never month first."""
    text = value.strip()
    # A timestamp such as 2024-03-14T09:30:00 or "2024-03-14 09:30:00".
    text = re.sub(r"^(\d{4}-\d{2}-\d{2})[T ].*$", r"\1", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise RowProblem(f"{what} {value!r} is not a date such as 14/03/2024 or 2024-03-14.")


_NUMBER_NOISE = re.compile(r"^(?:[A-Z]{3}\s*)?\$?")


def parse_decimal(value: str, what: str) -> Decimal:
    """An exact number. Accepts "1,234.50", "$12.00" and "NZD 12.00"."""
    text = _NUMBER_NOISE.sub("", value.strip().upper()).replace(",", "").strip()
    if text.startswith("-$"):
        text = "-" + text[2:]
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise RowProblem(f"{what} {value!r} is not a number.") from None
    if not number.is_finite():
        raise RowProblem(f"{what} {value!r} is not a number.")
    return number


def _places(number: Decimal) -> int:
    exponent = number.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def parse_quantity(value: str, what: str = "quantity", *, allow_negative: bool = False) -> Decimal:
    number = parse_decimal(value, what)
    if number == 0 or (number < 0 and not allow_negative):
        requirement = "non-zero" if allow_negative else "more than zero"
        raise RowProblem(f"{what} must be {requirement}, not {value!r}.")
    if _places(number) > QUANTITY_PLACES:
        raise RowProblem(f"{what} {value!r} has more than {QUANTITY_PLACES} decimal places.")
    return number


def parse_price(value: str, what: str = "price") -> Decimal:
    """A unit price, rounded to 6 decimal places (half to even) to fit the database exactly.

    Brokers sometimes report an average price with more places than that. Rounding here
    rather than in the database keeps it visible and the same for every source.
    """
    number = parse_decimal(value, what)
    if number < 0:
        raise RowProblem(f"{what} cannot be negative ({value!r}).")
    return number.quantize(Decimal(1).scaleb(-MONEY_PLACES), rounding=ROUND_HALF_EVEN)


def parse_money(value: str, what: str) -> Decimal:
    """A cash amount such as a fee. Blank means zero."""
    if not value.strip():
        return Decimal(0)
    number = parse_decimal(value, what)
    if number < 0:
        # Some brokers show fees as negative cash. The sign carries no meaning for a fee.
        number = -number
    if _places(number) > MONEY_PLACES:
        raise RowProblem(f"{what} {value!r} has more than {MONEY_PLACES} decimal places.")
    return number


def invert_rate(value: str, what: str = "exchange rate") -> Decimal:
    """Turns "1 base = x foreign" into "1 foreign = y base", the way rates are stored."""
    rate = parse_decimal(value, what)
    if rate <= 0:
        raise RowProblem(f"{what} must be more than zero, not {value!r}.")
    return (Decimal(1) / rate).quantize(Decimal(1).scaleb(-RATE_PLACES), ROUND_HALF_EVEN)


def listing(exchange: str, code: str, currency: str | None = None) -> tuple[str, str, str]:
    """(exchange, code, currency) checked against each other.

    `currency` is the trade currency the file gives, if any. A trade on the NZX in US
    dollars means a column was misread, so it is refused rather than guessed at.
    """
    try:
        exchange = markets.exchange_code(exchange)
        code = markets.instrument_code(code)
    except InvalidInput as exc:
        raise RowProblem(str(exc)) from None
    expected = markets.EXCHANGE_CURRENCY[exchange]
    if currency:
        currency = currency.strip().upper()
        if currency != expected:
            raise RowProblem(
                f"{code} on the {exchange} trades in {expected}, but the row says {currency}."
            )
    return exchange, code, expected


# Fingerprints -----------------------------------------------------------------------------


def _canonical(value: object) -> str:
    if isinstance(value, Decimal):
        # 10, 10.0 and 10.00 are the same amount, so they give the same fingerprint.
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip().upper()


class Fingerprinter:
    """Stable references for rows that have no ID of their own.

    The reference is a hash of the row's meaning (not its formatting), so importing the
    same file again, or a later export that overlaps it, finds the trades already stored.
    Two genuinely identical rows in one file (the same buy twice on one day) are told apart
    by counting them: the second one gets "-2".
    """

    def __init__(self) -> None:
        self._seen: Counter[str] = Counter()

    def ref(self, *fields: object) -> str:
        digest = hashlib.sha256("\x1f".join(_canonical(f) for f in fields).encode()).hexdigest()
        key = digest[:40]
        self._seen[key] += 1
        return f"{key}-{self._seen[key]}"
