# vendored stripe billing subset

- source: https://github.com/stripe/openapi
  - `openapi/spec3.json` via https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json
  - `openapi/fixtures3.json` via https://raw.githubusercontent.com/stripe/openapi/master/openapi/fixtures3.json
- fetched 2026-11-11; the downloads had the same sha256 as the ones recorded in
  `tests/fixtures/stripe/README.md` (commit `6360907ca278d6aecff118df90167e94aaba1437`):
  spec3.json `7cff4cc46d0654101a36a3302f7544640f5f73e4f3cad6f264f0e23c19fa1776`,
  fixtures3.json `c94c6c106a8743e559609792d2591c9b975e5588b4ea70425ce9377c1c6d0026`
- api version in the spec: `2026-09-30.endive`
- licence: MIT, copyright Stripe, Inc. (see `LICENSE`, copied verbatim)

## files

- `spec3.billing.json`: the `POST /v1/billing/meter_events` operation (form-encoded
  request: `event_name`, `payload`, `identifier`, `timestamp`) and the
  `billing.meter_event`, `error` and `api_errors` schemas, verbatim. `api_errors`
  refers to schemas not copied here (payment intents and the like); scruple reads only
  its `type` and `message`.
- `fixtures3.billing.json`: the `billing.meter_event` fixture object, verbatim.
- `extract.py`: rebuilds both files from a fresh download.

Not in the spec, from Stripe's public API reference: the `Idempotency-Key` request
header (https://docs.stripe.com/api/idempotent_requests). The meter event's own
`identifier` is deduplicated by Stripe "within a rolling period of at least 24 hours"
and `timestamp` must be "within the past 35 calendar days or up to 5 minutes in the
future" (both quoted from the spec's field descriptions).
