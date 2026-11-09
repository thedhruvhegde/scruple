# vendored stripe subset

- source: https://github.com/stripe/openapi
  - `openapi/spec3.json` via https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json
  - `openapi/fixtures3.json` via https://raw.githubusercontent.com/stripe/openapi/master/openapi/fixtures3.json
- commit: `6360907ca278d6aecff118df90167e94aaba1437` (master), fetched 2026-11-09
- api version in the spec: `2026-09-30.endive`
- sha256 of the full downloads: spec3.json `7cff4cc46d0654101a36a3302f7544640f5f73e4f3cad6f264f0e23c19fa1776`,
  fixtures3.json `c94c6c106a8743e559609792d2591c9b975e5588b4ea70425ce9377c1c6d0026`
- licence: MIT, copyright Stripe, Inc. (see `LICENSE`, copied verbatim)
- stripe-mock's own `openapi/fixtures3.json` returned 404 at its current head; the
  fixtures here come from stripe/openapi, which is what stripe-mock is generated from.

## files

- `spec3.subset.json`: the schemas scruple reads, verbatim (`dispute`, `dispute_evidence`,
  `dispute_evidence_details`, `dispute_payment_method_details`(`_card`), `balance_transaction`,
  `fee`, `charge`, `event`, `notification_event_data`, and, for the rule-relevant charge
  fields, `payment_method_details`, `payment_method_details_card`,
  `payment_method_details_card_checks`, `three_d_secure_details_charge`, `shipping`,
  `address`, `refund`), the `GET /v1/disputes` and
  `GET /v1/disputes/{dispute}` operations, and `x-scruple-dispute-event-types`
  (the `charge.dispute.*` values of the webhook endpoint `enabled_events` enum).
- `fixtures3.subset.json`: the `dispute`, `charge`, `balance_transaction`, `event` and
  `refund` fixture objects, verbatim.
- The charge-detail schemas and the `refund` fixture were added on 2026-11-09 from the
  same commit: a fresh download that day had the same sha256 for both files as above.
- `extract.py`: rebuilds both files from a fresh download.

Tests derive extra cases (for example a dispute with balance transactions) by editing
copies of these real objects in memory; nothing derived is written back here.
