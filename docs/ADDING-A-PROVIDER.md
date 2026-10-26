# Adding a broker importer

Start with the broker's supported file format or read-only API. Record which fields are
available, their currency and unit conventions, and whether the source provides history
or just a current position. Akahu is a confirmation source, not a trade importer. Other
providers are currently parked; do not add a Sharesight integration.

1. Put the parser/client in its own module. Accept streams or inject HTTP transport so tests
   use made-up fixtures without network access. Never commit broker exports or real data.
   Read credentials only from env/.env using `SecretStr`. Use safe errors that never echo
   request headers, URLs containing credentials, upstream bodies or row contents.
2. Parse money, FX and units directly as `Decimal`, never through `float`. Validate finite
   values, signs, currency and supported precision before writing. Treat a sell quantity
   as positive; use signed quantities only for adjustments. Record fees and trade date
   according to the broker's documented convention.
3. Resolve an `Instrument` by exchange and code. Require a manual override for ambiguous
   or unknown securities. Never merge instruments by display name. Keep provider IDs
   separate from exchange codes.
4. Use `ImportRun` to audit each import and `Trade`/`Income` for actual history. Use a stable
   broker reference with the existing `(portfolio_id, source, external_ref)` uniqueness
   constraint for idempotency. A content hash helps identify repeated files. Validate first,
   then commit the import transaction atomically; never leave half an import behind.
   Define how corrections and undo preserve financial history before exposing commands.
5. For a holdings-only source, persist confirmation snapshots and call the shared
   `portfolio_digest.reconcile.ledger_units` and `reconcile` functions, as `akahu_sync` does.
   Compare the union of instruments. Unknown holdings are unresolved drift, not zero units.
   Confirm the source covers the same accounts as the target portfolio. Never manufacture
   transactions to make a snapshot match.
6. Add a CLI subcommand with clear output, non-zero exits for drift/failure and optional
   success monitoring only after the transaction commits. Integrate the scheduler if one
   exists, otherwise document a nightly entry alongside price fetching.
7. Test duplicate imports, malformed records, fractional units, currencies, buy/sell signs,
   transfers, DRP, ambiguous instruments and transaction rollback. For HTTP sources test
   pagination, bounded rate-limit retry, partial failures and secret redaction. Include
   both missing and excess units in reconciliation tests. Add migrations for schema changes.
8. Document setup, supported source versions, account scope, known refresh delays and how
   users can resolve errors. Follow `docs/WORKING-AGREEMENT.md`, review financial accuracy,
   privacy and failure modes, then run the affected tests and ruff checks.

Use the existing `importers.base` parser interface and helpers, register file parsers in
`importing.FILE_IMPORTERS`, and use the shared import workflow for validation, deduplication
and undo. See [IMPORTING.md](IMPORTING.md) and the Sharesies and ASB implementations for
examples. Holdings-only providers should share reconciliation rather than create a
separate ledger or their own unit comparison algorithm.
