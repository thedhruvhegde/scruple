# Integrations (`scruple/ingest`, `scruple/security`)

Adapters turn external records into scruple's internal records
(`scruple.ingest.models`: `ExternalCase`, `LedgerMovement`). They hold observable facts
only: an imported case has no hidden ground truth, unlike the simulator's `DisputeCase`.

Rules every adapter follows:

- Every input is hostile: byte limits before decoding, JSON depth limits, strict
  UTF-8, no NaN, no duplicate keys, no `eval`, XML through defusedxml only.
- A parser returns a value or raises a subclass of `scruple.ingest.errors.IngestError`.
  Hypothesis fuzz tests check that nothing else escapes.
- Unknown enum values raise `UnmappedValueError`; nothing is guessed or silently dropped.
- Test mode first: live keys and live-mode objects are refused unless `allow_live=True`.
- Free text is not stored. Stripe evidence is recorded as the names of the fields present.
- No test makes a network call. HTTP clients take an `httpx` transport.
- Status, stage and category mappings are scruple's modelling choices (ASSUMPTION),
  not processor or network rules. Network reason codes are kept verbatim, never mapped.

## Stripe disputes (`scruple.ingest.stripe`)

- Source of truth: the real Stripe OpenAPI spec, vendored subset in
  `tests/fixtures/stripe/` (stripe/openapi at `6360907`, API `2026-09-30.endive`, MIT).
- `wire.py`: Pydantic models for the fields scruple reads. Unknown fields are ignored
  (Stripe adds fields without a version bump); integers and booleans are strict.
- Contract tests (`tests/test_ingest_stripe.py`): every declared field exists in the
  spec with a matching type, spec-optional fields are optional, spec-nullable fields
  accept None; `STATUS_MAP` equals the `dispute.status` enum; `REASON_MAP` equals the
  reasons listed in `dispute.reason`; `EVIDENCE_FIELDS` equals `dispute_evidence`.
- Mapping: status to (stage, state); `fraudulent`, `product_not_received`,
  `product_unacceptable` to the three scruple categories, all other reasons to None with
  the raw reason kept; `evidence_details.due_by == 0` means no response allowed.
- Balance transactions become `LedgerMovement`s (gross, fee, net, with
  `net == amount - fee` checked, as documented by Stripe) and can be posted into the
  simulator's double-entry `Ledger` against `processor:disputes` and `processor:fees`.
- `StripeClient`: read-only, `GET /v1/disputes` (cursor pagination with
  `starting_after`/`has_more`, bounded by `max_pages`) and `GET /v1/disputes/{id}`.
  Only `sk_test_`/`rk_test_` keys by default; key never in `repr`; ids are checked
  against `^[A-Za-z0-9_]+$` before they reach a URL; responses are size-limited.
- Not done: writes (submitting evidence, closing disputes), retries and rate-limit
  backoff, Connect (`Stripe-Account` header), `expand[]`.

## Stripe webhooks (`scruple.ingest.stripe.webhook`, route in `scruple.ingest.api`)

- Scheme ported from Stripe's own library (stripe/stripe-python `stripe/_webhook.py`
  at `ee9db33`, MIT): `Stripe-Signature: t=...,v1=...`, hex HMAC-SHA256 of
  `"{t}.{raw body}"` keyed with the `whsec_...` secret, any `v1` may match (rotation),
  `v0` ignored, constant-time compare, 300 s default tolerance.
- Stricter than the library (our choice): future timestamps beyond the tolerance are
  rejected; exactly one numeric `t`; `v1` must be 64 lowercase hex; header length and
  signature count are bounded; every (secret, signature) pair is compared.
- Replay: event ids are remembered (bounded LRU); a repeat is acknowledged (200,
  `duplicate: true`) and not processed again. A delivery that failed to map is not
  remembered, so Stripe's retry can succeed later.
- Vectors in the tests were computed independently with `openssl dgst -sha256 -hmac`.
- Route: `POST /ingest/stripe/webhook`, mounted in `api/app.py` with one line.
  Secrets from `SCRUPLE_STRIPE_WEBHOOK_SECRETS` (comma separated); unset means 503.
  400 bad signature, 413 body over 2 MiB, 422 signed but unmappable.
- Stubbed: accepted batches stay in memory on the receiver; persisting them (through
  the service layer) is not written yet.

## Adyen dispute notifications (`scruple.ingest.adyen`)

- HMAC ported from Adyen's library (Adyen/adyen-python-api-library `Adyen/util.py` at
  `2b708a4`, MIT) and checked against its own vectors: the item vector in
  `test/UtilTest.py`, the four `test/mocks/util` notifications (colons, slashes,
  backslashes, unescaped) and the header-signed payload vector.
- Signing string: `pspReference:originalReference:merchantAccountCode:merchantReference:
  value:currency:eventCode:success`, key hex-decoded, base64 HMAC-SHA256, constant time.
  Several keys may be configured (rotation). One bad item rejects the whole delivery.
- Shape and event list from Adyen/adyen-openapi `json/Webhooks-v1.json` at `d449f09`
  (MIT). `EVENT_MAP` equals the 19 webhooks tagged `Dispute` (contract test).
  Note: that spec's `eventCode` enum omits six of those webhooks (`PREARBITRATION_ACCEPTED`,
  `_DECLINED`, `_ISSUER_WITHDRAWN`, `SCHEME_ARBITRATION`, `_WON`, `_LOST`), so the wire model
  keeps `eventCode` as a string and `EVENT_MAP` decides.
- ASSUMPTION: (stage, state) per event is our reading of each webhook's one-line summary;
  `disputeStatus`, when present, overrides the state (`Expired`/`Undefended` read as LOST).
  `PREARBITRATION_ACCEPTED` is read as merchant-accepted; the summary does not say who.
- Items that are not dispute events, or carry `success: "false"`, are returned in
  `skipped` with a reason. The free-text `reason` field is never stored.
- `live: "true"` notifications are refused unless `allow_live=True`.
- Not done: an HTTP route for Adyen (the adapter is a function), Basic auth on the
  endpoint, the `[accepted]` acknowledgement, the Disputes API client.

## CSV and JSONL case import (`scruple.ingest.tabular`)

- `ImportConfig` (Pydantic, frozen, extra forbidden) declares the column for each
  `ExternalCase` field, the amount format (`decimal` parsed exactly with the ISO 4217
  exponent, or `minor` integers), and the `status_map` / `reason_map` from the
  source's values to scruple's. Two fields may not share a column.
- File-level problems (missing or duplicate columns, unmapped extra columns unless
  `allow_extra_columns`, empty file, bad UTF-8, NUL bytes, too many bytes or rows)
  raise. Row-level problems become `RowError(row, column, message)`; all problems in
  a row are reported, not just the first. `strict=True` raises `ImportFailedError`
  carrying the full report.
- No silent drops: `rows_read == len(cases) + rows that failed` (hypothesis property).
  Duplicate ids are errors that name the first row. A CSV quoting error stops reading
  at that row (Python's csv module cannot resynchronise) and is reported there.
- Timestamps need a UTC offset; a plain `YYYY-MM-DD` date means midnight UTC.
- Values are only ever strings: nothing is evaluated (`=1+1` stays text).
- Parquet is deferred: pyarrow is a large native dependency for one format. Convert to
  CSV or JSONL first.
- Currency table: `scruple.ingest.currency`, from datasets/currency-codes at `31cfa13`
  (ODC-PDDL-1.0; list published by SIX Group as ISO 4217 maintenance agency).

## Ledgers (`scruple.ingest.ledger`)

- `LedgerPort` protocol: `post` (idempotent by `external_id`; same id with different
  content raises `LedgerConflictError`), `get`, `transactions`, `balance`.
- `LedgerTransaction`: signed lines in one currency that sum to zero (negative = money
  leaving an account), at least two non-zero lines, aware timestamp, account addresses
  `segment(:segment)*` of letters, digits, `_` and `-`.
- Adapters: `InMemoryLedger`; `JsonFileLedger`, an append-only JSON Lines file of the
  generic double-entry document (`entries` with `direction` debit/credit and positive
  amounts), fsynced per post and re-validated line by line on open.
- Conversions: `from_movement` (Stripe balance movements), `from_journal` (the
  simulator's `JournalEntry`), `to_json_doc`/`from_json_doc`.
- Formance: `to_formance`/`from_formance` for the v3 `CreateTransactionRequest`
  (`postings` of `source`, `destination`, `amount`, `asset` such as `USD/2`; `reference`,
  `timestamp`, `metadata`), verified against formancehq/ledger `openapi.yml` at `c0fe3f5`.
  Signed lines are decomposed greedily into source to destination postings; round trips
  preserve each account's net position (property test). Bigint amounts as decimal strings
  are accepted. That repository has no licence file, so nothing from it is vendored.
- Modern Treasury: `to_modern_treasury` builds `LedgerTransactionCreateParams`
  (`ledger_entries` with `direction`, `ledger_account_id`, `amount`), verified against
  Modern-Treasury/modern-treasury-python at `cf80342` (MIT). ASSUMPTION: every account is
  treated as debit-normal (positive line = debit); real use needs each account's normal
  balance. Mapping stubs only: no Formance or Modern Treasury HTTP client.

## Bank statements (`scruple.ingest.bank`)

- Output: `BankStatement` (account, currency, balances, entries). Entry amounts are
  unsigned minor units plus a direction; `description` is the bank's free text, truncated
  to 500 characters, to be redacted before storage.
- BAI2 (`parse_bai2`): layout, continuation joining and integrity rules ported from
  ministryofjustice/bai2 at `6696a72` (MIT); passes its four real sample files. Account,
  group and file control totals, record counts, account and group counts are all checked
  (`check_integrity=False` must be asked for). Type code direction table ported from the
  same library. ASSUMPTION: amounts are minor units of the account currency; a missing
  currency is an error unless `default_currency` is given (the library assumes USD).
- camt.053 (`parse_camt053`): versions 001.02 to 001.13, defusedxml with DTDs, entities
  and external references forbidden (billion laughs, XXE and external DTD tests). Real
  public sample: prowide-iso20022 (Apache 2.0, header and balances only). The entry-level
  fixture is hand-made and labelled so. Amounts parsed exactly with the ISO 4217 exponent;
  more decimals than the currency allows is an error.
- CSV (`import_bank_csv`): declared columns, one signed amount column or separate debit
  and credit columns, ISO or day-first or month-first dates, row-level errors.
- Bank files are all-or-nothing for structure (control totals are file-level); the CSV
  importer reports per row.

## NACHA ACH returns (`scruple.ingest.nacha`)

- Record layout, return addenda (type 99) and control rules from moov-io/ach at
  `c7025ec` (Apache 2.0), checked against its public test files (two returns files, one
  PPD debit). Return reason titles (R01 to R85) ported from its `addenda99.go`; unknown
  codes such as R97 parse with no title.
- Checked: every record exactly 94 ASCII characters (trimmed lines only with
  `allow_trimmed=True`); record order; file header constants; transaction codes; addenda
  indicator agrees with addenda present; return addenda trace equals its entry's trace;
  batch control (service class, entry/addenda count, entry hash mod 10^10, debit and
  credit totals, batch number); file control (batch count, block count, counts, hash,
  totals); only all-9 filler after the file control.
- Privacy: account number reduced to last four characters; receiver name and individual
  id are not kept.
- Tests: tampering of each control field, a generator of valid files (hypothesis round
  trip, including entry hash overflow), single-byte mutation fuzzing.
- Not done: IAT and ADV formats, notification of change (addenda 98) contents, writing files.
