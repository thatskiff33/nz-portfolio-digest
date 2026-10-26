"""The "Sharesight format" trades CSV: a catch-all for any broker that exports one.

Many brokers offer an export "for Sharesight". This only reads that file format; nothing
here talks to Sharesight itself. The columns we read (headers may say "(optional)"):

    Trade Date, Instrument Code, Market Code, Quantity, Price in Dollars,
    Transaction Type, Exchange Rate, Brokerage, Brokerage Currency, Comments

- Transaction types: BUY, SELL and OPENING_BALANCE. Others (splits, bonus issues and so on)
  are refused with a clear error: enter those with `trade add --type adjustment`.
- `Price in Dollars` is in the market's own currency.
- `Exchange Rate` is how much of that currency one unit of the portfolio's currency buys
  (for a US share in an NZ$ portfolio, the NZD/USD rate such as 0.6). We store the inverse.
- `Brokerage` is in `Brokerage Currency`, or the market's currency when that is blank. A
  fee in the portfolio's currency is converted using the row's exchange rate.
"""

from decimal import DecimalException

from portfolio_digest.importers.base import (
    Fingerprinter,
    Importer,
    ParseContext,
    ParsedTrade,
    ParseResult,
    RowError,
    RowProblem,
    checked_text,
    invert_rate,
    listing,
    parse_date,
    parse_decimal,
    parse_money,
    parse_price,
    parse_quantity,
    read_csv,
)
from portfolio_digest.models import TradeType

COLUMNS = {
    "date": ("Trade Date", "Date"),
    "code": ("Instrument Code", "Code", "Symbol"),
    "market": ("Market Code", "Market", "Exchange"),
    "quantity": ("Quantity", "Units"),
    "price": ("Price in Dollars", "Price"),
    "type": ("Transaction Type", "Type"),
    "rate": ("Exchange Rate",),
    "fee": ("Brokerage", "Fees"),
    "fee_currency": ("Brokerage Currency",),
    "comments": ("Comments", "Notes"),
}
REQUIRED = {"date", "code", "market", "quantity", "price", "type"}

_TYPES = {
    "BUY": TradeType.BUY,
    "SELL": TradeType.SELL,
    "OPENING_BALANCE": TradeType.OPENING_BALANCE,
}


class SharesightCsvImporter(Importer):
    name = "sharesight-csv"
    trade_source = "sharesight_csv"
    summary = 'Any broker\'s "Sharesight format" trades CSV'

    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        result = ParseResult()
        refs = Fingerprinter()
        for line, row in read_csv(content, COLUMNS, REQUIRED):
            where = f"line {line}"
            try:
                result.trades.append(self._trade(where, row, context, refs))
            except RowProblem as exc:
                result.errors.append(RowError(where, str(exc)))
        return result

    @staticmethod
    def _trade(
        where: str, row: dict[str, str], context: ParseContext, refs: Fingerprinter
    ) -> ParsedTrade:
        kind_text = "_".join(row["type"].upper().split())
        kind = _TYPES.get(kind_text)
        if kind is None:
            raise RowProblem(
                f"transaction type {row['type']!r} is not supported here (BUY, SELL or "
                "OPENING_BALANCE). Enter it with `trade add --type adjustment` instead."
            )
        exchange, code, currency = listing(row["market"], row["code"])
        trade_date = parse_date(row["date"], "Trade Date")
        quantity = parse_quantity(row["quantity"], "Quantity")
        price = parse_price(row["price"], "Price", currency=currency)
        fee_currency = (row.get("fee_currency") or currency).strip().upper()
        fees = parse_money(row.get("fee", ""), "Brokerage", currency=fee_currency)

        base = context.base_currency
        foreign_per_base = None
        fx_rate = None
        if currency != base and row.get("rate"):
            fx_rate = invert_rate(row["rate"], "Exchange Rate")
            foreign_per_base = parse_decimal(row["rate"], "Exchange Rate")

        if fees and fee_currency != currency:
            if fee_currency != base or foreign_per_base is None:
                raise RowProblem(
                    f"Brokerage is in {fee_currency} but the trade is in {currency}, and there "
                    "is no exchange rate to convert it."
                )
            try:
                fees = parse_price(str(fees * foreign_per_base), "Brokerage")
            except DecimalException:
                raise RowProblem("Brokerage is outside the supported money range.") from None

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
            fx_base=base if fx_rate is not None else None,
            external_ref=refs.ref(trade_date, kind, exchange, code, quantity, price, fees),
            notes=checked_text(" ".join(row.get("comments", "").split()), "Comments") or None,
        )
