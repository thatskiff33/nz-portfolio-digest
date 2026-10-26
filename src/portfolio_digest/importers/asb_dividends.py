"""ASB dividend advice, extracted text or an RFC 822 email, one advice per file."""

from portfolio_digest.importers.asb import email_text
from portfolio_digest.importers.base import (
    FileProblem,
    Importer,
    ParseContext,
    ParseResult,
    RowError,
    RowProblem,
    checked_text,
    decode_text,
    listing,
)
from portfolio_digest.income import parse_income

LABELS = {
    "statement reference": "reference",
    "security code": "code",
    "market": "market",
    "currency": "currency",
    "payment date": "date",
    "ex date": "ex_date",
    "gross dividend": "gross",
    "net dividend": "net",
    "withholding tax": "tax",
    "resident withholding tax": "rwt",
    "rwt": "rwt",
    "foreign withholding tax": "foreign_tax",
    "imputation credits": "imputation",
    "franking credits": "franking",
    "drp quantity": "drp",
    "income kind": "kind",
}


class AsbDividendImporter(Importer):
    name = "asb-dividend"
    trade_source = "asb_securities"
    summary = "ASB dividend advice, extracted PDF text or saved email"

    def parse(self, content: bytes, context: ParseContext) -> ParseResult:
        text = decode_text(content)
        if any(line.lower().startswith("content-type:") for line in text.splitlines()):
            _, text = email_text(content)
        fields = {}
        for line in text.splitlines():
            label, sep, value = line.strip().partition(":")
            key = LABELS.get(label.lower())
            if sep and key:
                if key in fields:
                    raise FileProblem(f"Repeated dividend field: {label}.")
                fields[key] = value.strip()
        required = {"reference", "code", "market", "currency", "date", "gross", "net"}
        if missing := required - fields.keys():
            raise FileProblem("Dividend advice is missing: " + ", ".join(sorted(missing)))
        result = ParseResult()
        try:
            reference = checked_text(fields["reference"], "Statement reference")
            if not reference or len(reference) > 110:
                raise RowProblem("Statement reference must contain 1 to 110 characters.")
            exchange, code, currency = listing(fields["market"], fields["code"], fields["currency"])
            result.incomes.append(
                parse_income(
                    "advice",
                    fields,
                    exchange,
                    code,
                    currency,
                    "dividend:" + reference,
                )
            )
        except RowProblem as exc:
            result.errors.append(RowError("advice", str(exc)))
        return result
