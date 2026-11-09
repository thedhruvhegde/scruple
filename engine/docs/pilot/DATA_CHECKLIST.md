# Data checklist

What to export, how much, and what happens to it. Field names below are the ones the
scruple parsers read today, taken from the code (`scruple/ingest/stripe/wire.py`,
`scruple/ingest/adyen.py`, `scruple/ingest/tabular.py`). A field not listed here is not
read. If your export has extra fields, remove them before export: that is the simplest
way to make sure they never reach scruple.

## Pick one source

### A. Stripe disputes (`scruple.ingest.stripe`)

Export: dispute objects as returned by `GET /v1/disputes` (one JSON object per dispute,
or a list page). scruple's own client (`StripeClient`) is read-only (`GET /v1/disputes`,
`GET /v1/disputes/{id}`) and accepts only `sk_test_` / `rk_test_` keys unless the code
is called with `allow_live=True`; the mapper refuses live-mode objects the same way.
For a pilot on live history, either your engineer runs the export with your own
tooling and hands over JSON, or a restricted read-only key is used inside your
environment with `allow_live=True` set explicitly in the pilot's ingest step.

Fields read (API version of the vendored spec: `2026-09-30.endive`):

| object | fields |
|---|---|
| `dispute` | `id`, `object`, `amount`, `currency`, `created`, `charge`, `reason`, `status`, `livemode`, `is_charge_refundable`, `balance_transactions`, `evidence`, `evidence_details`, `payment_method_details`, `metadata` |
| `charge` (only if expanded instead of an id) | `id`, `amount`, `amount_captured`, `amount_refunded`, `currency`, `created`, `livemode` |
| `dispute.evidence_details` | `due_by`, `has_evidence`, `past_due`, `submission_count` |
| `dispute.payment_method_details` | `type`, `card.brand`, `card.case_type`, `card.network`, `card.network_reason_code` |
| `dispute.balance_transactions[]` | `id`, `object`, `amount`, `currency`, `created`, `fee`, `fee_details[].amount`, `fee_details[].currency`, `fee_details[].type`, `net`, `reporting_category`, `status`, `type` |
| `dispute.evidence` | **presence only** of the 27 documented evidence fields (`EVIDENCE_FIELDS` in `scruple/ingest/stripe/mapping.py`); the content is never stored |

Notes:

- `metadata` must be present (it can be `{}`); its values are not mapped or stored.
  Empty it before export if it holds anything personal.
- `evidence` is parsed in memory to see which fields are present; values such as
  `customer_name`, `customer_email_address`, `billing_address` or `customer_purchase_ip`
  pass through the parser even though only the field names are kept. To keep them in
  your environment, replace each non-empty evidence value with a placeholder string
  (for example `"present"`) before export. The result is the same.
- `reason` values outside Stripe's documented list, or unknown `status` values, are
  errors (`UnmappedValueError`), not guesses.

### B. Adyen dispute notifications (`scruple.ingest.adyen`)

Export: stored standard notifications (the `notificationItems` envelope with `live` and
`NotificationRequestItem` objects), as received by your webhook endpoint.

| level | fields |
|---|---|
| envelope | `live`, `notificationItems[].NotificationRequestItem` |
| item | `pspReference`, `originalReference`, `merchantAccountCode`, `merchantReference`, `eventCode`, `eventDate`, `success`, `amount.value`, `amount.currency` |
| `additionalData` | `hmacSignature`, `chargebackReasonCode`, `chargebackSchemeCode`, `rfiReasonCode`, `rfiSchemeCode`, `nofReasonCode`, `nofSchemeCode`, `defensePeriodEndsAt`, `disputeStatus` |

Notes:

- Signatures are verified by default (HMAC keys, several allowed for rotation). For
  stored history, verification needs the HMAC key; run ingest inside your environment so
  the key stays there, or agree in writing to ingest with `verify=False`.
- `live: "true"` notifications are refused unless `allow_live=True` is set explicitly.
- The free-text `reason` field is never stored. Non-dispute events and
  `success: "false"` items are listed as skipped with a reason, not dropped.
- At most 100 items per envelope (`MAX_ITEMS`).

### C. CSV or JSON Lines (`scruple.ingest.tabular`)

One row per dispute. You declare which column holds which field in an `ImportConfig`.

| field | required | format |
|---|---|---|
| `external_id` | yes | your dispute id, unique per file |
| `payment_ref` | yes | charge, PSP or transaction id |
| `amount` | yes | decimal in major units (`12.50`, parsed exactly with the ISO 4217 exponent), or integer minor units with `amount_format = "minor"` |
| `currency` | yes | ISO 4217 code |
| `status` | yes | your value, mapped by `status_map` to a (stage, state) pair |
| `reason` | yes | your value, mapped by `reason_map` to `fraud`, `not_received`, `not_as_described` or none (kept verbatim) |
| `opened_at` | yes | ISO 8601 with a UTC offset, or a plain `YYYY-MM-DD` (read as midnight UTC) |
| `merchant_ref` | no | merchant or sub-merchant id |
| `network` | no | card network name, kept verbatim |
| `network_reason_code` | no | network reason code, kept verbatim, never mapped |
| `evidence_due_at` | no | same format as `opened_at` |

Rules the importer enforces: unmapped extra columns are an error unless
`allow_extra_columns` is set; every bad row is reported with its row number, column and
message; nothing is evaluated (a cell `=1+1` stays text); limits of 64 MiB and 100,000
rows per file by default. Parquet is not supported: convert to CSV first.

### Fields the rule replay also needs (planned)

The rule language (`scruple.domain.FIELD_TYPES`) decides on these observable fields.
No adapter reads them yet; mapping them is part of the week 1 build (SCOPE.md). Each
source column is agreed with your engineer and written down in the `ImportConfig` or
the mapping code, with the choice marked as ours.

| rule field | type | where it usually comes from (to confirm with your engineer) |
|---|---|---|
| `reason_code` | category | already mapped (above) |
| `amount_usd_minor` | integer | `amount` plus `currency`; non-USD needs an FX rate source you specify |
| `currency_mismatch` | bool | dispute currency differs from settlement currency |
| `mcc` | string | merchant category code from your merchant records |
| `three_ds` | bool | whether the original payment was 3-D Secure authenticated |
| `delivery_confirmed` | bool | carrier delivery confirmation on file |
| `evidence_on_time` | bool | evidence was (or could be) submitted before the due date |
| `prior_dispute_on_txn` | bool | another dispute exists on the same payment |
| `has_prior_refund` | bool | the payment was partly or fully refunded before the dispute |

## Date range and volume

- Resolved disputes only (won, lost, accepted, closed). Open cases have no outcome and
  do not count toward the sample size.
- Window: the most recent `[M]` months, where `M` is at least the planner's
  `months of resolved history` (SUCCESS_CRITERIA.md). With the planner's illustrative
  inputs that was 0.8 months; with a worst-case variance it was more than a decade. Run
  the planner with your own inputs before agreeing the window.
- Leave a gap of at least your median time to final outcome between the window's end
  and the export date, so cases near the end have resolved.
- Balance movements (fees, debits, reversals) for every exported dispute, so cost is
  read from money movement, not from a status field.

## How personal data is handled

What the code does today (all in `scruple/ingest` and `scruple/security`, tests named
after each module in `tests/`):

- **Not read at all:** card numbers (no adapter has a PAN field), cardholder names,
  email addresses, addresses, Stripe evidence content, Adyen `reason` text, NACHA
  receiver names and individual ids.
- **Redaction** (`scruple.security.redact`): `redact_text` replaces emails, Luhn-valid
  card numbers, IBANs, long numbers, phone numbers, IP addresses, common address
  patterns and cued names with placeholders; `redact_record` replaces known PII keys
  whole. Bank statement descriptions are redacted when the record is built. Known
  limits (tested and pinned): a bare name with no cue and not in `known_names` is not
  found; addresses outside the listed patterns are not found.
- **Card tokens** (`scruple.security.pan`): if a file you send does contain a card
  number, `PanTokenizer` turns it into a keyed HMAC token plus first six, last four and
  length. The key stays with whoever runs ingest. A property test writes tokens to a
  SQLite file and logs and checks the PAN never appears.
- **Field encryption** (`scruple.security.crypto`): AES-256-GCM per field, bound to the
  record and field it belongs to, with key ids and rotation. Removing a key makes its
  ciphertexts unreadable.
- **Not yet wired:** redaction, tokenisation and encryption are library functions. The
  ingest command that calls them on every row is part of the week 1 build. scruple's
  storage today holds rules, decisions and the audit log, not imported cases.

## What never leaves your environment

Recommended default: scruple runs inside your environment (Docker image, SECURITY.md),
so none of the data leaves. If instead the work runs on a scruple-operated machine,
these stay with you regardless:

- API keys (Stripe restricted keys, Adyen HMAC keys) and webhook secrets;
- the tokenisation key and the field-encryption keys;
- raw exports before redaction and the removal of unused fields;
- evidence documents and free text;
- anything that identifies a cardholder.

What may leave, only if agreed: redacted case records with tokens instead of
identifiers, scorecards, evidence packs and the readout.

## Deletion at the end

- Within `[N]` days of the readout, scruple deletes every copy of partner data it holds
  and confirms in writing: imported cases, ingest reports, backtest and shadow outputs,
  and the database files used for the pilot.
- The audit log is append-only by design (database triggers refuse UPDATE and DELETE).
  Deleting pilot data therefore means deleting the whole pilot database (SQLite file or
  Postgres schema), not removing rows.
- Encrypted fields can also be destroyed by deleting their keys (crypto-shredding).
- No retention-policy code exists yet (ROADMAP 155); deletion is a manual, documented
  step listed in the readout.
- The readout itself, scorecards and evidence packs are kept only as the terms allow.
