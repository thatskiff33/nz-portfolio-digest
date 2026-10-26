"""Sharesies "Transaction Report" CSV (in Sharesies: Account > Generate Reports).

One row per filled order, across the NZX, ASX and US markets, including Smart ETFs and
fractional shares. The columns we read:

    Order ID, Trade date, Instrument code, Market code, Quantity, Price,
    Transaction type, Exchange rate, Transaction fee, Currency, Amount, Transaction method

- `Price` and `Transaction fee` are in the row's `Currency` (the market's own currency).
- `Exchange rate` is blank for NZ$ trades. For others it is how much of the foreign
  currency NZ$1 bought, e.g. 0.6 for US dollars; we store the inverse (NZ$ per US$1).
- `Amount` (the cash total) is not needed: quantity, price and fee say it all.
- Rows that are not buys or sells (dividends, for example) are skipped with a notice.
- Share transfers in or out are skipped with a notice. Their units come from the other
  broker's history, so importing them here would count them twice.
"""

from portfolio_digest.importers.base import (
    Fingerprinter,
    Importer,
    ParseContext,
    ParsedTrade,
    ParseResult,
    RowError,
    RowProblem,
    invert_rate,
    listing,
    parse_date,
    parse_money,
    parse_price,
    parse_quantity,
    read_csv,
)
from portfolio_digest.models import TradeType

COLUMNS = {
    "order_id": ("Order ID",),
    "date": ("Trade date",),
    "code": ("Instrument code",),
    "market": ("Market code",),
    "quantity": ("Quantity",),
    "price": ("Price",),
    "type": ("Transaction type",),
    "rate": ("Exchange rate",),
    "fee": ("Transaction fee",),
    "currency": ("Currency",),
    "method": ("Transaction method",),
}
REQUIRED = {"date", "code", "market", "quantity", "price", "type", "currency"}

_TYPES = {"BUY": TradeType.BUY, "SELL": TradeType.SELL}


class SharesiesImporter(Importer):
    name = "sharesies"
    trade_source = "sharesies"
    summary = "Sharesies Transaction Report CSV (Account > Generate Reports)"

    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        result = ParseResult()
        refs = Fingerprinter()
        for line, row in read_csv(content, COLUMNS, REQUIRED):
            where = f"line {line}"
            kind = row["type"].upper()
            method = row.get("method", "")
            if kind not in _TYPES:
                result.skipped.append(RowError(where, f"{row['type'] or 'blank'} is not a trade"))
                continue
            if "TRANSFER" in method.upper():
                result.skipped.append(
                    RowError(where, f"{method}: share transfers are counted from the other broker")
                )
                continue
            try:
                result.trades.append(self._trade(where, row, _TYPES[kind], refs))
            except RowProblem as exc:
                result.errors.append(RowError(where, str(exc)))
        return result

    @staticmethod
    def _trade(
        where: str, row: dict[str, str], kind: TradeType, refs: Fingerprinter
    ) -> ParsedTrade:
        exchange, code, currency = listing(row["market"], row["code"], row["currency"])
        trade_date = parse_date(row["date"], "Trade date")
        quantity = parse_quantity(row["quantity"], "Quantity")
        price = parse_price(row["price"], "Price")
        fees = parse_money(row.get("fee", ""), "Transaction fee")
        fx_rate = None
        if currency != "NZD" and row.get("rate"):
            fx_rate = invert_rate(row["rate"], "Exchange rate")
        method = row.get("method", "")
        return ParsedTrade(
            where=where,
            exchange=exchange,
            code=code,
            trade_date=trade_date,
            trade_type=kind,
            quantity=quantity,
            price=price,
            currency=currency,
            fees=fees,
            fx_rate=fx_rate,
            fx_base="NZD" if fx_rate is not None else None,
            external_ref=refs.ref(
                row.get("order_id", ""), trade_date, kind, exchange, code, quantity, price
            ),
            notes=None if method.upper() in ("", "ORDER") else f"Sharesies: {method}",
        )
