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
- Payment facts (`mapping.payment_facts`, `ExternalCase.facts`): read from the dispute
  and, when `charge` is expanded (`expand[]=data.charge` on the list), from the charge:
  `payment_method_details.card.three_d_secure` (`result` verbatim, `authenticated` only
  for `result == "authenticated"`, every other value in the spec enum is "not
  authenticated", `three_d_secure: null` means not used, a `result` the spec does not
  list is None), `.checks` (AVS line 1, AVS postal code, CVC, verbatim), `.brand`,
  `.funding`, `.country`, `shipping` (only whether an address and a tracking number are
  present, and the carrier name after redaction; never the address, name or phone),
  and `refunds` (refunded before the dispute opened: from refund timestamps, False when
  `amount_refunded == 0`, None when the list is not expanded or incomplete; refund state
  at export). Dispute-level: `is_charge_refundable`, evidence counters,
  `enhanced_eligibility_types`. MCC is not in the charge or dispute schema: always None.
  Every field the export does not carry is None, never guessed. The vendored spec subset
  now includes `payment_method_details`, `payment_method_details_card`,
  `payment_method_details_card_checks`, `three_d_secure_details_charge`, `shipping`,
  `address` and `refund` (same commit; contract tests cover them, including the inline
  `charge.refunds` list).
- Pull from the API (`scruple ingest stripe-pull`, `scruple.ingest.stripe.pull`):
  `--since D --until D --out export.jsonl [--store cases.db --report I.json]`. Pages
  `GET /v1/disputes` with `created[gte]`, `created[lt]` and `expand[]=data.charge`,
  100 per page; key from `STRIPE_API_KEY` only (test keys only unless `--allow-live`,
  live-mode objects refused too). Evidence values become presence markers and the
  expanded charge is cut to the `Charge` wire fields (address and tracking number as
  presence markers) before the page is written, so no free text, name, email or
  address reaches the file. Resumable: a cursor file (`OUT.cursor.json`) holds the
  window, last id, byte length and sha256 of the export after every page; a rerun
  continues, cuts any bytes written after the last cursor, refuses a cursor for
  another window or an export that no longer matches (`--restart`), and is a no-op
  once complete. Exit 3 means incomplete (`--max-pages`), rerun to resume. With
  `--store` the finished export goes through the ordinary ingest pipeline.
  `StripeClient(max_retries=...)` retries 429, 5xx and transport errors with
  exponential backoff (`backoff_s * 2**attempt`, or `Retry-After` if larger) and
  stops on `Stripe-Should-Retry: false`; the pull uses 5 retries. Tests: a fake
  Stripe serving the vendored fixtures through `httpx.MockTransport`
  (tests/test_ingest_stripe_pull.py); never run against Stripe itself here.
- Not done: writes (submitting evidence, closing disputes), Connect (`Stripe-Account`
  header), expanding `payment_intent` (the mapper does not read it).

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
  Secrets from `SCRUPLE_STRIPE_WEBHOOK_SECRETS` (comma separated; `tenant=secret` maps a
  secret to a tenant, a bare secret is `SCRUPLE_INGEST_TENANT`'s); unset means 503.
  400 bad signature, 413 body over 2 MiB, 422 signed but unmappable.
- Persisted to the signing tenant's case store when one is configured
  (`SCRUPLE_CASE_STORE`, `SCRUPLE_CASE_STORES`), idempotent by event id; without one,
  verified and acknowledged only.

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
- Payment facts (`item_facts`): `paymentMethod` as the card brand, `paymentMethodVariant`
  verbatim (funding is not inferred from it), `defendable` and `autoDefended` (the
  strings "true"/"false"; anything else is None). The Webhooks-v1 dispute schema has no
  3-D Secure, AVS/CVC, MCC, shipping or refund fields, so those are None.
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
- Optional fact columns: `mcc` (verbatim), and `three_ds`, `delivery_confirmed`,
  `prior_refund` as true/false/yes/no/1/0; an empty cell is None (unknown), anything
  else is a row error.
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

## PII redaction (`scruple.security.redact`)

- `redact_text`: deterministic, idempotent placeholders `[EMAIL]`, `[PAN]` (13 to 19
  digits passing Luhn), `[IBAN]` (mod-97 checked), `[NUMBER]` (other runs of 12+
  digits), `[PHONE]`, `[IP]` (validated by `ipaddress`), `[ADDRESS]` (street suffix,
  PO box, UK postcode, US state plus ZIP), `[NAME]` (by cue such as "my name is", "Mr",
  "cardholder", "Attn:", or any caller-supplied `known_names`, matched loosely).
- Adversarial formats handled: NFKC normalisation (fullwidth digits and symbols),
  zero-width and bidi format characters removed, separators inside card numbers
  (space, dash, dot, newline), `[at]`/`(at)`/` at ` and `[dot]` email obfuscation.
- `redact_record`: known PII keys (name, email, phone, address, card number, IP and
  variants, matched case- and separator-insensitively) are replaced whole; other
  strings are scanned; integers that are Luhn-valid PANs are replaced; depth-limited.
- Bank statement entry descriptions are redacted when the model is constructed.
- ReDoS: every regex quantifier is bounded and input is capped at 200,000 characters;
  tests run hostile repetitions under a time bound.
- Limits (tested and pinned): a bare name with no cue and not in `known_names` is not
  found; addresses outside the listed patterns are not found; long non-card numbers
  are redacted as `[NUMBER]` (over-redaction is preferred to leaks).

## Card numbers (`scruple.security.pan`)

- `luhn_valid` (ISO/IEC 7812-1 Annex B), `normalise_pan` (NFKC, separators and format
  characters removed, 13 to 19 digits, Luhn required). Errors never echo the input.
- `PanTokenizer`: keyed HMAC-SHA256 with key ids (keys of 32 bytes or more). The same
  card and key give the same `ctok_...` token, so cases on one card can be linked; an
  unkeyed hash of a 16-digit PAN could be brute-forced, a keyed one cannot without the
  key. `CardToken` keeps token, key id, first six (`bin6`), last four and length only.
  ASSUMPTION: first six plus last four is the common truncation format; whether it is
  acceptable is a compliance decision for each deployment.
- Rotation: `matches` accepts tokens from any configured key; `rotate` re-issues under the
  active key and needs the PAN, so it only happens at ingest. Dropping a key retires its
  tokens.
- Logs: `PanScrubbingFilter` rewrites log messages and arguments with Luhn-valid card
  numbers replaced; the module's own logger has it installed.
- Property test over generated PANs (several brands, 13 to 19 digits, separators):
  tokenise, log, write to a SQLite file, then search the bytes and the log text for the
  PAN. It never appears.

## Field-level encryption (`scruple.security.crypto`)

- `FieldCipher`: AES-256-GCM (`cryptography`), 32-byte keys with ids, a fresh 96-bit
  random nonce per encryption. Envelope: `enc:v1:<key id>:<nonce>:<ciphertext+tag>`
  (base64url), storable in any text column.
- Associated data binds version, key id and a caller context (record and field), so a
  ciphertext moved to another field or record, or relabelled with another key id (even
  one holding the same key bytes), fails authentication.
- Rotation: `rotate` and `rotate_record` re-encrypt under the active key; old keys stay
  readable while in the ring; removing a key crypto-shreds its envelopes.
- NIST SP 800-38D section 8.3: with random nonces, keep one key under 2**32
  encryptions. Nothing counts encryptions yet; rotate on a schedule.
- Tests: an independent `AESGCM` decrypt of a pinned vector, a flip of every ciphertext
  byte and of the nonce, context swaps, key relabelling, truncation, malformed envelopes,
  rotation and shredding, hypothesis round trips and parser fuzzing.
- Wired into `scruple ingest` (below) for chosen columns or paths, keys from
  `SCRUPLE_FIELD_KEYS`.
- Not done: key storage (KMS, envelope keys wrapped by a KEK), per-tenant keys, and
  counting encryptions per key.

## The ingest command and the case store (`scruple.ingest.cli`, `.pipeline`, `.store`)

```bash
uv run scruple ingest stripe --in disputes.json --store cases.db --report ingest.json
uv run scruple ingest adyen  --in notifications.json --store cases.db   # SCRUPLE_ADYEN_HMAC_KEYS
uv run scruple ingest csv    --in cases.csv --store cases.db --config import.json \
    --pan-column card_number --encrypt-column customer_email           # SCRUPLE_TOKEN_KEYS, _FIELD_KEYS
uv run scruple ingest jsonl  --in cases.jsonl --store cases.db --config import.json
uv run scruple ingest verify --store cases.db [--k 10] [--json]
```

- Per record: limits, then sensitive columns out first (card numbers tokenised with the
  keyed `PanTokenizer`, chosen columns or dotted paths sealed with `FieldCipher` bound to
  `case:<case id>:<field>`, card numbers scrubbed from a value before it is sealed), the
  adapter's mapping, redaction of the text kept (`merchant_ref`, `network`,
  `network_reason_code`, `source_reason`, shipping carrier), and a final check that the
  clear-text part of the record holds no Luhn-valid 13 to 19 digit run. An id holding a
  card number or email address rejects the row (an id cannot be redacted). Card numbers
  in input fields that are not kept are counted by field name in the report.
- Keys only from the environment (`scruple.ingest.keys`): `SCRUPLE_TOKEN_KEYS` and
  `SCRUPLE_FIELD_KEYS` as `kid:base64url,...` (first is active), `SCRUPLE_ADYEN_HMAC_KEYS`
  as hex. A card-number column without a tokenisation key, or an encrypted column without
  a field key, fails those rows; nothing is stored another way.
- Row-level report: every record is stored or listed as an error with row, column and
  message (`rows_read == rows_ok + rows_failed`, hypothesis property). Exit 0 all stored,
  1 some rows failed (the rest stored; `--strict` stores nothing), 2 the file or options
  were rejected. `--dry-run` validates only.
- The store is its own SQLite file, separate from the gate database. Append-only
  (triggers refuse UPDATE and DELETE); a re-import with new content (a dispute since won)
  is a new version, readers take the latest; an identical re-import is a no-op. A store
  holds synthetic or real data, never both.
- `ingest verify`: counts per source, state, stage, reason, currency, token key id and
  encrypted field, all through the calibration suppression (k = 10 by default, primary
  and complementary), envelope decryption checks when a field key is set, and a scan of
  every text value in the file for card numbers (must be 0; exit 1 otherwise).
- `load_store` turns a store into a backtest `History` (`--format store` for
  `scruple backtest`, `--history` for `scruple ci run`).
- Tests: `tests/test_ingest_pipeline.py` (the no-PAN property over generated CSVs with
  card numbers in every column, raw file bytes searched; append-only; key parsing that
  never echoes keys; fuzzing), `tests/test_ingest_payment_facts.py`.

## Backtest and calibration (`scruple.backtest`, `scruple.calibration`)

- Imported histories feed the backtest and the aggregates-only calibration: docs/BACKTEST.md.

## Policy files (`scruple.policy`, `scruple policy`)

- One versioned file per tenant: YAML, TOML or JSON, `policy_version: 1`. Validated by
  pydantic with unknown keys refused at every level (a typo is an error).
- Sections: `stats` (provider `bootstrap`, `evalue` or `evalue_vr`, plus alpha, beta,
  confidence, resamples, band edges), `case_types` (per case-type tolerance and minimum
  detectable regression, keyed `scenario` or `scenario/<$80`; evalue providers only;
  scenario and band checked against the gate's case types), `fairness` (verifier
  profile: merchant, balanced, compliance), `approval` (an `ApprovalPolicy`), `canary`
  (a `RampSchedule`).
- Strict reading: 256 KiB cap, UTF-8 only, YAML via a `SafeLoader` subclass that refuses
  duplicate keys (PyYAML keeps the last one silently), JSON duplicates refused too.
- `policy_sha256`: SHA-256 of the canonical JSON of the validated policy. YAML, TOML and
  JSON spellings, in any key order, hash the same.
- Compiled onto `GateConfig` (`Policy.gate_config`), which then carries `policy_sha256`.
  The hash ends up in every `decision.recorded` audit entry made under the policy, every
  scorecard and every evidence pack. With no policy the field is left out, so existing
  config hashes, scorecards and packs stay byte-identical.
- Per tenant, append-only: table `tenant_policies` (created on first apply, never
  updated). Each apply is audited as `policy.set` (number, name, hash, previous hash).
  Applying the policy already in force is a no-op.
- An `approval` section is applied through `AgentGate.set_approval_policy`, so only a
  gate operator (`GateConfig.approvers`) can apply such a policy; a refusal writes nothing.
- The API's per-tenant gates (`TenantGates`) read the current hash on each request and
  rebuild the gate when it changes, so a policy applied from the CLI or another replica
  applies on the next request.
- Routes: `GET /policy` (read), `GET /policy/history` (read), `POST /policy` (admin).
- CLI: `scruple policy validate FILE...` (exit 1 if any invalid), `show FILE|--current`,
  `diff OLD NEW` (either may be `@current`; exit 1 when they differ), `apply FILE --actor`,
  `history`.
- Not done: the fairness profile is carried and hashed but the gate's own checks do not
  read it (the verifier profiles score episodes, `scruple.verifiers.profiles`); the canary
  ramp from the policy is returned by `Policy.ramp()` for whoever starts a ramp. Example
  thresholds are configuration, not a statement of what any institution requires
  (ASSUMPTION).

## Outbound integrations: how events leave (`scruple.integrations`)

- The audit log is the outbox. A gate change and its audit entry commit together; the
  dispatcher (`scruple worker --dispatch-integrations`, every 5 s, or
  `scruple integrations dispatch`) reads a tenant's entries after its cursor, stores
  `GateEvent`s and queues jobs with idempotency keys derived from the event. A rolled
  back change sends nothing; a crash between queueing and moving the cursor queues
  nothing twice; the gate never waits on, or fails because of, a receiver.
- Events: `decision.recorded`, `approval.requested`, `approval.granted`,
  `approval.satisfied`, `approval.rejected`. Ids are `evt_` plus 24 hex of
  SHA-256(tenant, audit entry hash). Decision events carry the summary, reasons, owner,
  agent, report path, `policy_sha256` when a policy is in force, and `data: synthetic`.
- A tenant's first setting or endpoint starts the cursor at the current audit head:
  history is not replayed to a new receiver.
- Per-tenant settings (`IntegrationSettings`: `slack`, `jira`, `linear`) are versioned
  and audited (`integration.settings_set`). They name secrets by environment variable,
  and the variable must start with the tenant's prefix, `SCRUPLE_SECRET_<TENANT>_`
  (tenant upper-cased, `-` to `_`), so one tenant's admin cannot aim an integration at
  the database password or another tenant's token.
- Outbound HTTP (`safehttp.SafeHttp`): https only; no credentials, fragments or odd
  ports in the URL; every resolved address must be public (private, loopback,
  link-local including 169.254.169.254, CGNAT, multicast, reserved, unspecified and
  IPv4-mapped addresses are refused); the request is pinned to the checked address
  (`Host` and TLS SNI keep the name), so DNS rebinding between check and connect does
  not work; redirects are not followed; proxies from the environment are ignored;
  responses are capped at 64 KiB. `SCRUPLE_INTEGRATIONS_ALLOW_HOSTS` exempts named
  hosts from the address check; `SCRUPLE_INTEGRATIONS_DEV=1` allows http (development).
- Tests never use the network: `Runtime(http=SafeHttp(transport=httpx.MockTransport(...),
  resolver=...))` and `make_handlers(runtime=...)` with a thread-isolated worker.

## Outbound webhooks (`scruple.integrations.webhooks`)

- Endpoints per tenant: `POST /integrations/webhooks {url, events, description}`
  (admin), or `scruple integrations webhooks add URL`. The URL is checked at
  registration and again on every delivery. Disable, enable and rotate are audited.
- Signing secret: `whsec_` plus 32 random bytes, shown once (at creation and at
  rotation), stored sealed with `SCRUPLE_FIELD_KEYS` (AES-256-GCM, bound to tenant and
  endpoint). Without field keys, registration answers 503 (development mode stores it
  unsealed). Rotation signs with the new and the old secret for 24 hours.
- Request: `POST`, body the canonical JSON of the event (sorted keys, no spaces) with
  `api_version: 1`. Headers: `Scruple-Signature: t=<unix>,v1=<hex>[,v1=<hex>]` where
  `v1 = hex(HMAC-SHA256(secret, "<t>.<raw body>"))`, plus `Scruple-Event-Id`,
  `Scruple-Event-Type`, `Scruple-Delivery-Id`, `Scruple-Attempt`.
- Retries: one `webhook_deliver` job per (endpoint, event). Network errors, 408, 429
  and 5xx are retried with the job queue's exponential backoff (2 s doubling, capped at
  300 s) for up to 8 attempts; any other status, a URL that now fails the address
  check, or a disabled endpoint fails at once. Deliveries are at least once:
  deduplicate on `Scruple-Event-Id`.
- Log and replay: `webhook_deliveries` (status, attempts, last status and error) and
  `webhook_attempts` (every attempt with time, status, error, duration and job).
  `GET /integrations/webhooks/deliveries[?status=failed]`,
  `GET .../deliveries/{id}`, `POST .../deliveries/{id}/replay` (audited, same event id).
- Verifying a delivery (receiver side, standard library only; tested against the
  signer in `tests/test_integrations_webhooks.py`):

```python
# scruple-webhook-verify
import hashlib
import hmac
import time


def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    """True if `header` (Scruple-Signature) signs `body` (the raw bytes) with `secret`."""
    fields = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamps = [v for k, v in fields if k == "t"]
    sigs = [v for k, v in fields if k == "v1"]
    if len(stamps) != 1 or not stamps[0].isdigit() or not sigs:
        return False
    if abs(time.time() - int(stamps[0])) > tolerance:
        return False  # too old or from the future: a replay
    expected = hmac.new(
        secret.encode(), stamps[0].encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)
```
