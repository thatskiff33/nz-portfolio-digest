"""ASB Securities: trade-confirmation emails (contract notes) and the historical statement.

ASB Securities has no CSV export for share trades, so there are two ways in:

1. Contract-note emails, one per trade. These arrive in Gmail and are read from a
   single label (see `portfolio_digest.gmail`), or a saved `.eml` file can be imported.
2. The trade statement PDF, for history from before the emails. Extract its text with
   `pdftotext -layout` and import the `.txt` file.

Both carry ASB's contract note number, which becomes the trade's reference. A trade that
appears in both an email and the statement is therefore only stored once.

The layouts below are what this parser expects. If ASB changes theirs, the parser refuses
the email or row with a clear message rather than guessing.

Contract note email (plain text or HTML, one "Label: value" per line or table row):

    Contract note number:  40012345
    Trade date:            14/03/2024
    We have BOUGHT the following securities on your behalf:   (or SOLD)
    Security code:         FPH
    Security name:         Fisher & Paykel Healthcare Corporation Limited
    Market:                NZX          (optional; NZX unless the currency is AUD)
    Quantity:              100
    Price:                 NZD 27.5000
    Consideration:         NZD 2,750.00 (optional; checked against quantity x price)
    Brokerage:             NZD 30.00
    GST:                   NZD 0.00     (optional; added to brokerage)

Trade statement text, one trade per line under a header:

    Trade Date  Contract No  Type  Code  Market  Quantity  Price  Brokerage  Consideration
    14/03/2016  30001234     Buy   FPH   NZX     100       9.8500   30.00      985.00

An optional GST column may appear between Brokerage and Consideration. Fees are the sum
of Brokerage and GST; without a separate GST column, Brokerage must be the total fee.
"""

import email
import email.policy
import re
from decimal import Decimal
from email.message import EmailMessage
from html.parser import HTMLParser

from portfolio_digest.importers.base import (
    FileProblem,
    Importer,
    ParseContext,
    ParsedTrade,
    ParseResult,
    RowError,
    RowProblem,
    checked_money,
    decode_text,
    listing,
    parse_date,
    parse_decimal,
    parse_money,
    parse_price,
    parse_quantity,
    parse_security_name,
)
from portfolio_digest.models import TradeType

TRADE_SOURCE = "asb_securities"


class NotAContractNote(Exception):
    """The email is not an ASB contract note at all (no contract note number)."""


def _places(text: str) -> int:
    _, _, fraction = text.strip().partition(".")
    return len(fraction)


def _check_consideration(quantity: Decimal, price_text: str, consideration: Decimal) -> None:
    """Catches a misread column: quantity x price must come to the stated consideration.

    The printed price is rounded, so allow half a unit in its last place for every share,
    plus a cent.
    """
    price = parse_decimal(price_text, "price")
    slack = Decimal("0.01") + quantity * Decimal(5).scaleb(-_places(price_text) - 1)
    if abs(quantity * price - consideration) > slack:
        raise RowProblem(
            f"quantity {quantity} x price {price} does not come to the consideration "
            f"{consideration}, so a value has probably been misread."
        )


def _ref(contract: str) -> str:
    contract = contract.strip()
    # Leave room for the "contract:" prefix in Trade.external_ref (128 characters).
    if not re.fullmatch(r"[0-9]{1,119}", contract):
        raise RowProblem("contract number must contain 1 to 119 digits.")
    return "contract:" + contract


# Contract-note emails ---------------------------------------------------------------------


class _HtmlText(HTMLParser):
    """HTML to text: one line per row or paragraph, a tab between table cells."""

    _BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BREAKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_data(self, data):
        self.parts.append(" ".join(data.split()) if data.strip() else "")


def _html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return "\n".join(line.strip("\t ") for line in "".join(parser.parts).splitlines())


def email_text(raw: bytes) -> tuple[str, str]:
    """(subject, body as plain text) of an RFC 822 email."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if not isinstance(message, EmailMessage):  # pragma: no cover - the policy guarantees it
        raise FileProblem("This is not an email.")
    subject = str(message.get("Subject", ""))
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return subject, ""
    try:
        body = part.get_content()
    except (LookupError, UnicodeError):
        raise FileProblem("The email's text is in an encoding we cannot read.") from None
    if part.get_content_type() == "text/html":
        body = _html_to_text(body)
    return subject, body


_LABEL_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z .'/&-]*?)\s*(?::|\t+)\s*(\S.*?)\s*$")

_LABELS = {
    "contract": ("contract note number", "contract note no", "contract number", "contract no"),
    "date": ("trade date", "transaction date", "date of trade"),
    "code": ("security code", "stock code", "code"),
    "name": ("security name", "security", "company"),
    "market": ("market", "exchange"),
    "quantity": ("quantity", "units", "number of shares"),
    "price": ("price", "average price", "price per share"),
    "consideration": ("consideration", "gross amount"),
    "brokerage": ("brokerage",),
    "gst": ("gst", "gst on brokerage"),
    "type": ("transaction type", "buy/sell", "order type"),
}
_LABEL_LOOKUP = {alias: ours for ours, aliases in _LABELS.items() for alias in aliases}
_DIRECTION = re.compile(r"\b(BOUGHT|SOLD)\b", re.IGNORECASE)
_CURRENCY_PREFIX = re.compile(r"^\s*([A-Z]{3})\b")


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = _LABEL_LINE.match(line)
        if not match:
            continue
        ours = _LABEL_LOOKUP.get(" ".join(match.group(1).lower().split()))
        if ours and ours not in fields:
            fields[ours] = match.group(2)
    return fields


def _direction(fields: dict[str, str], body: str) -> TradeType:
    stated = fields.get("type", "").upper()
    if stated in ("BUY", "BOUGHT"):
        return TradeType.BUY
    if stated in ("SELL", "SOLD"):
        return TradeType.SELL
    found = {m.upper() for m in _DIRECTION.findall(body)}
    if found == {"BOUGHT"}:
        return TradeType.BUY
    if found == {"SOLD"}:
        return TradeType.SELL
    raise RowProblem("cannot tell whether this was a buy or a sale.")


def parse_contract_note(raw: bytes, where: str) -> ParsedTrade:
    """One trade from one contract-note email.

    Raises `NotAContractNote` for other emails and `RowProblem` for a contract note that
    cannot be read.
    """
    subject, body = email_text(raw)
    fields = _fields(body)
    if "contract" not in fields:
        raise NotAContractNote(f"{where}: not an ASB contract note ({subject or 'no subject'})")
    missing = [_LABELS[f][0] for f in ("date", "code", "quantity", "price") if f not in fields]
    if missing:
        raise RowProblem(f"contract note is missing: {', '.join(missing)}.")

    currency_match = _CURRENCY_PREFIX.match(fields["price"].upper())
    currency = currency_match.group(1) if currency_match else None
    market = fields.get("market") or ("ASX" if currency == "AUD" else "NZX")
    exchange, code, currency = listing(market, fields["code"], currency)

    quantity = parse_quantity(fields["quantity"], "Quantity")
    price_text = _CURRENCY_PREFIX.sub("", fields["price"].upper()).replace("$", "").strip()
    price = parse_price(fields["price"], "Price", currency=currency)
    if "consideration" in fields:
        _check_consideration(
            quantity,
            price_text,
            parse_money(fields["consideration"], "Consideration", currency=currency),
        )
    fees = checked_money(
        parse_money(fields.get("brokerage", ""), "Brokerage", currency=currency)
        + parse_money(fields.get("gst", ""), "GST", currency=currency),
        "Fees",
    )
    return ParsedTrade(
        where=where,
        exchange=exchange,
        code=code,
        trade_date=parse_date(fields["date"], "Trade date"),
        trade_type=_direction(fields, body),
        quantity=quantity,
        price=price,
        currency=currency,
        fees=fees,
        name=parse_security_name(fields.get("name")),
        external_ref=_ref(fields["contract"]),
    )


class AsbEmailImporter(Importer):
    name = "asb-email"
    trade_source = TRADE_SOURCE
    summary = "One ASB Securities contract-note email saved as an .eml file"

    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        result = ParseResult()
        try:
            result.trades.append(parse_contract_note(content, "email"))
        except NotAContractNote:
            raise FileProblem(
                "This email is not an ASB Securities contract note (no contract note number)."
            ) from None
        except RowProblem as exc:
            result.errors.append(RowError("email", str(exc)))
        return result


# Historical trade statement ---------------------------------------------------------------

_STARTS_WITH_DATE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\b")
_NUMBER = r"[\d,]+(?:\.\d+)?"
_STATEMENT_ROW = re.compile(
    r"^\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<contract>\S+)\s+"
    r"(?P<type>buy|sell|bought|sold)\s+"
    r"(?P<code>\S+)\s+"
    r"(?P<market>[A-Za-z]+)\s+"
    rf"(?P<quantity>{_NUMBER})\s+"
    rf"(?P<price>{_NUMBER})\s+"
    rf"(?P<brokerage>{_NUMBER})\s+"
    rf"(?:(?P<gst>{_NUMBER})\s+)?"
    rf"(?P<consideration>{_NUMBER})\s*$",
    re.IGNORECASE,
)


class AsbStatementImporter(Importer):
    name = "asb-statement"
    trade_source = TRADE_SOURCE
    summary = "ASB Securities trade statement, as text from `pdftotext -layout`"

    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        result = ParseResult()
        for number, line in enumerate(decode_text(content).splitlines(), start=1):
            if not _STARTS_WITH_DATE.match(line):
                continue  # headings, page footers, totals
            where = f"line {number}"
            match = _STATEMENT_ROW.match(line)
            if match is None:
                result.errors.append(
                    RowError(where, f"cannot read this trade row: {' '.join(line.split())}")
                )
                continue
            try:
                result.trades.append(self._trade(where, match))
            except RowProblem as exc:
                result.errors.append(RowError(where, str(exc)))
        if not result.trades and not result.errors:
            raise FileProblem(
                "No trades found. Is this the text of an ASB Securities trade statement?"
            )
        return result

    @staticmethod
    def _trade(where: str, row: re.Match[str]) -> ParsedTrade:
        exchange, code, currency = listing(row["market"], row["code"])
        quantity = parse_quantity(row["quantity"], "Quantity")
        price = parse_price(row["price"], "Price", currency=currency)
        _check_consideration(
            quantity,
            row["price"],
            parse_money(row["consideration"], "Consideration", currency=currency),
        )
        kind = TradeType.BUY if row["type"].upper() in ("BUY", "BOUGHT") else TradeType.SELL
        return ParsedTrade(
            where=where,
            exchange=exchange,
            code=code,
            trade_date=parse_date(row["date"], "Trade Date"),
            trade_type=kind,
            quantity=quantity,
            price=price,
            currency=currency,
            fees=checked_money(
                parse_money(row["brokerage"], "Brokerage", currency=currency)
                + parse_money(row["gst"] or "", "GST", currency=currency),
                "Fees",
            ),
            external_ref=_ref(row["contract"]),
        )
