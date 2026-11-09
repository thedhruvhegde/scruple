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
| `charge` (only if expanded instead of an id: `expand[]=data.charge`) | `id`, `amount`, `amount_captured`, `amount_refunded`, `currency`, `created`, `livemode`, `refunded`, `payment_method_details.type`, `.card.brand`, `.card.funding`, `.card.country`, `.card.checks` (`address_line1_check`, `address_postal_code_check`, `cvc_check`), `.card.three_d_secure` (`result`, `result_reason`, `version`, `authentication_flow`), `shipping` (whether `address.line1`/`postal_code` and `tracking_number` are present, and `carrier`), `refunds.data[]` (`id`, `amount`, `created`, `status`), `refunds.has_more` |
| `dispute` (also) | `enhanced_eligibility_types` |
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

### Fields the rule replay reads

The rule language (`scruple.domain.FIELD_TYPES`) decides on these observable fields.
They are read into `ExternalCase.facts` (`scruple.ingest.models.PaymentFacts`) when the
export carries them; a field the export lacks is None (unknown), the backtest uses a
fixed default for the rule, and the report counts every case where it did.

| rule field | type | Stripe | Adyen | CSV/JSONL column |
|---|---|---|---|---|
| `reason_code` | category | mapped from `reason` | none (codes kept verbatim) | `reason` via `reason_map` |
| `amount_usd_minor` | integer | `amount` + `currency` (synthetic FX table: USD, EUR, GBP only) | same | same |
| `three_ds` | bool | expanded charge: `three_d_secure.result == "authenticated"`; `three_d_secure: null` is false; a null or unknown result is unknown | not in the dispute notification schema | `three_ds` |
| `has_prior_refund` | bool | expanded charge: a refund created before the dispute (false if `amount_refunded == 0`, unknown if the refund list is missing or paged) | not in the schema | `prior_refund` |
| `mcc` | string | not in the charge or dispute schema | not in the schema | `mcc` |
| `delivery_confirmed` | bool | not in the schema (shipping presence and tracking presence are kept as facts, not as confirmation) | not in the schema | `delivery_confirmed` |
| `evidence_on_time` | bool | `evidence_details.due_by` after the decision time | `defensePeriodEndsAt` | `evidence_due_at` |
| `prior_dispute_on_txn` | bool | another dispute on the same `charge`, opened earlier | same `originalReference` | same `payment_ref` |
| `currency_mismatch` | bool | not read (default false) | not read | not read |

Also kept as facts (not rule fields): card funding and country, 3-D Secure version,
AVS and CVC check results, refund state at export, Adyen `paymentMethodVariant`,
`defendable`, `autoDefended`.

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
- **Wired into ingest:** `scruple ingest` applies all three to every record before it is
  stored: `--pan-column` tokenises card numbers (key `SCRUPLE_TOKEN_KEYS`),
  `--encrypt-column` / `--encrypt-path` seals chosen fields (key `SCRUPLE_FIELD_KEYS`),
  and the text that is kept is redacted. The case store refuses any record whose clear
  text still holds a card number, and `scruple ingest verify` scans the whole file for
  one. Card numbers in fields that are not kept are counted by field name in the ingest
  report, never stored. Tested: a hypothesis property generates CSVs with card numbers
  in every column and searches the store file's bytes (tests/test_ingest_pipeline.py).

What it looks like on a file with a card-number column, run here on a hand-made
SYNTHETIC CSV (`4242...` is the public test card number), first without the key:

```
$ uv run scruple ingest csv --in cards.csv --store cases.db --config import.json --pan-column card
csv: 2 records read, 0 ok, 2 failed; 0 cases (0 new, 0 updated, 0 unchanged), 0 movements; 0 card numbers tokenised, 0 fields encrypted, 0 redactions; run 1
  row 1: a card-number column needs a tokenisation key (SCRUPLE_TOKEN_KEYS); nothing was stored
  row 2: a card-number column needs a tokenisation key (SCRUPLE_TOKEN_KEYS); nothing was stored
$ SCRUPLE_TOKEN_KEYS="k1:$(python -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
    uv run scruple ingest csv --in cards.csv --store cases.db --config import.json --pan-column card
csv: 2 records read, 2 ok, 0 failed; 2 cases (2 new, 0 updated, 0 unchanged), 0 movements; 2 card numbers tokenised, 0 fields encrypted, 1 redactions; run 2
$ grep -c -a 4242424242424242 cases.db; grep -c -a 5555555555554444 cases.db
0
0
$ uv run scruple ingest verify --store cases.db --k 2
...
card tokens: 2
  token key ids: k1 2
...
raw card numbers in the file: 0
STORE OK
```

(The one redaction is the email address in the `merchant` column, stored as `[EMAIL]`.
`--k 2` only because the file has two rows; the default is 10.)

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
