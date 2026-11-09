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
  Note: that spec's `eventCode` enum omits five of those webhooks (`PREARBITRATION_ACCEPTED`,
  `_DECLINED`, `_ISSUER_WITHDRAWN`, `SCHEME_ARBITRATION`, `_WON`/`_LOST`), so the wire model
  keeps `eventCode` as a string and `EVENT_MAP` decides.
- ASSUMPTION: (stage, state) per event is our reading of each webhook's one-line summary;
  `disputeStatus`, when present, overrides the state (`Expired`/`Undefended` read as LOST).
  `PREARBITRATION_ACCEPTED` is read as merchant-accepted; the summary does not say who.
- Items that are not dispute events, or carry `success: "false"`, are returned in
  `skipped` with a reason. The free-text `reason` field is never stored.
- `live: "true"` notifications are refused unless `allow_live=True`.
- Not done: an HTTP route for Adyen (the adapter is a function), Basic auth on the
  endpoint, the `[accepted]` acknowledgement, the Disputes API client.
