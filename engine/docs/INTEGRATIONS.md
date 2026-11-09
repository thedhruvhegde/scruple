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
