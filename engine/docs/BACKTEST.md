# Backtest and calibration on a partner's own history (153, 154)

The day-one pilot question: *would this new rule or agent have done better on our own
past disputes, and how sure are we?* The partner exports their dispute history from
Stripe, Adyen or a CSV, and scruple replays every case against the candidate.

```bash
uv run scruple backtest run --history disputes.json --format stripe \
    --candidate rule.json --out report/          # report.json, report.md, report.pdf
uv run scruple backtest calibrate --history disputes.json --format stripe \
    --out calibration.json --profile-out profile.json
uv run scruple backtest synth --n 5000 --seed 7 --out SYNTHETIC.json   # labelled test data
uv run scruple backtest run --history cases.db --format store \
    --candidate rule.json --out report/          # a `scruple ingest` case store
```

The same backtest gates CI (`scruple ci run --history`, below) and prices the
estimated metrics of shadow mode on imported cases (`scruple.gate.shadow_imported`).

Code: `scruple/calibration/` (153), `scruple/backtest/` (154). Tests:
`tests/test_calibration_*.py`, `tests/test_backtest_*.py`.

Nothing in this repository is a partner's data. The end-to-end tests run on edited
copies of Stripe's published fixtures (`tests/fixtures/stripe`, stripe/openapi at
`6360907`, MIT) and on a SYNTHETIC 5,000-case history; every report on synthetic data
says SYNTHETIC in its title, its dataset name and its JSON (`"synthetic": true`).

## Method

1. **Load** the export with the ingest adapters (`scruple.ingest`; every input rule in
   docs/INTEGRATIONS.md applies). Stripe balance transactions become double-entry
   `LedgerTransaction`s and are posted into an `InMemoryLedger`, which checks balance
   and idempotency.
2. **Calibrate** (153): aggregate the history with small-cell suppression, fit a
   `CalibrationProfile` from the aggregates only, and report the divergence of the
   default and the fitted generator from the history (below).
3. **Decide**: for each resolved chargeback, build its decision-time view and ask the
   candidate. `None` keeps the historical action.
4. **Cost** each case under history and under the candidate, labelled `observed` or
   `estimated` (below). Costs are minor units of one currency (the most common one
   unless `--currency`); other currencies are excluded and counted.
5. **Test** with `scruple.stats` through `scruple.stats.gate_adapter.evaluate_pairs`:
   one loss pair per case (history, candidate), the reason as the case type, caps
   declared from amounts and fees. Then per-reason improvement e-processes with e-BH,
   sample-size planning, and the same test at both ends of every estimate.

Cases left out, and counted on the report: inquiries and prevented disputes (no
accept-or-contest decision), open chargebacks (no outcome yet), other currencies.

## Reading an export (ASSUMPTION)

`scruple/calibration/facts.py`, used by both steps:

- contested: evidence was submitted; or, unless the merchant accepted, the case reached
  representment or later, or it was won;
- Stripe has no "accepted" status: accepting closes a dispute as `lost`. A lost
  dispute with no submission reads as accepted. A missed deadline looks the same.
- Adyen: events are folded per payment (`originalReference`); a case that ever reached
  representment or later counts as contested.
- CSV: there are no evidence columns, so map contested outcomes to a representment (or
  later) stage in `status_map`, and merchant acceptances to state `accepted`.

## The no-look-ahead guarantee

`scruple/backtest/view.py`. Every fact about a case carries the time it became known;
`build_view(case, index, at)` keeps the facts known at or before `at` (the open time,
plus `decision_delay_hours`) and drops the rest. Dropped facts are not stored in the
view at all, so a policy cannot reach them through private attributes; naming one
(`view.state`, `view["evidence_submitted"]`, `view.get(...)`) raises `LookAheadError`,
which fails the run.

| known at decision time | never known before the outcome |
|---|---|
| reason (verbatim and scruple category), network, network reason code | final state and stage |
| amount, currency, open time, evidence due date, response allowed | evidence submitted, which fields, late or not |
| earlier disputes on the same payment (opened strictly before) | the historical action |
| ledger postings up to the decision time | later postings (reinstatement credits) |
| payment facts: card brand, funding, country; 3-D Secure result; AVS and CVC checks; MCC; shipping address and tracking present; delivery confirmed (ASSUMPTION: before the dispute); refunded before the dispute | refund state and amount at export, refundability, evidence counters, enhanced eligibility, Adyen `defendable` |

Exports do not timestamp submissions or outcomes, so those are treated as unknown until
the outcome. Rule candidates read the observable-field dictionary of
`scruple.domain.FIELD_TYPES`. `mcc`, `delivery_confirmed`, `three_ds` and
`has_prior_refund` come from the payment facts when the export has them (an expanded
Stripe charge, CSV fact columns; docs/INTEGRATIONS.md); otherwise, and always for
`currency_mismatch`, fixed defaults are used (`mcc=unknown`, the rest false), and the
report counts per field how many cases used a default (`rule_fields_defaulted`). Tests: every outcome fact
raises; a policy that peeks fails the backtest; a hypothesis property checks that no
fact in any view is later than its decision time; two histories that differ only in
outcomes get identical rule decisions.

## Observed vs estimated

| candidate vs history | label | candidate cost |
|---|---|---|
| same action | observed | the recorded cost: merchant net outflow in the ledger (debits plus fees minus reinstatements); without postings, the amount if lost or accepted, else 0 |
| accept where history contested | estimated | amount plus fees posted by the decision time (the chargeback stands) |
| contest where history accepted | estimated | fees by decision time + `--contest-fee` + (1 - p_win) x amount, p_win from the calibrated profile for the reason; band from its 95% Wilson interval |
| escalate | estimated | the historical cost plus `--escalation-cost` (default: scruple.sim's synthetic analyst cost) |

Every report splits counts and costs into observed and estimated, overall and per
reason. The verdict is recomputed with the pessimistic and the optimistic end of every
estimate; `robust` is true when it does not move.

## Statistics and verdicts

- **worse**: the adapter's block family flags a reason or the pool (weighted
  e-Bonferroni of betting e-processes; FWER at most `alpha_block`, 0.05).
- **better**: overall, the pooled certificate (`mean diff < 0`, e-value at least
  `1/alpha_certify`); per reason, a betting e-process for "candidate cheaper" per reason
  combined by e-BH (FDR at most `alpha_fdr`, 0.1).
- **no change**: the candidate never differs from history (all observed).
- **insufficient evidence**: none of the above. The report gives, per reason, the cases
  `scruple.stats.plan` says are needed to flag a regression of 5% of the reason's cap with
  power 0.8, and marks reasons with too few cases.

Each reason row also has the mean difference, a time-uniform confidence sequence and
both e-values. Caps come from amounts and fees (raised if an observed cost exceeds
them); a single very large case widens its reason's cap and costs power.

## Calibration from aggregates only (153)

`compute_aggregates` is the only calibration code that reads rows. It returns counts of
at least `k` (default 10), rates whose successes and failures are each at least `k`, and
means over at least `k` cases: reason mix (small reasons pooled as `__other__`),
categories, networks, inquiry and open shares, contest and win rates by reason, win
rates with and without each evidence field per reason, deadlines (on time, late, not
submitted), amounts on fixed bins (edges chosen in advance, never from the data) with
log-amount moments, and the mean dispute fee. Suppression is primary plus complementary:
if the pooled small cells would sum to between 1 and k - 1, the smallest published cells
join them, so no small cell is "total minus the rest". `fit_profile` takes only that
object; a missing parameter falls back to pooled aggregates, then to the default
profile, and is listed in `fallbacks`. An evidence field becomes a reason's key field
only when its lift is clear of noise (two-proportion z of at least 3).

Divergence: total variation for the reason mix and the amount bins, absolute
differences for rates and log-amount moments, `overall` their mean. "Before" draws a
SYNTHETIC history from the default profile, "after" from the fitted one, and compares
their aggregates with the history's.

`to_case_mix(profile)` carries the inquiry share and network weights into
`scruple.sim`'s generator (opt-in). The simulator's amount model, reason catalogue and
issuer odds are its own; the counterfactual win rates come from the calibration profile,
not from `scruple.sim.DisputeEngine`, whose outcomes depend on documents that exports
do not contain.

Tests: suppression never emits a cell below `k` (hypothesis over tables and over whole
histories); no id, reference or timestamp appears in the aggregates; aggregates are
order-free and deterministic; the fit recovers known parameters within about five
standard errors (hypothesis over seeds and parameters, n = 8,000); calibration lowers
divergence; the Stripe rendering of a synthetic history maps back to the same records.

## What a partner exports

**Stripe** (`--format stripe`): the `GET /v1/disputes` list (every page; a list with
`has_more: true` is refused), as a JSON array, a list object, or JSON Lines; with
`expand[]=data.charge` the payment facts are read too. Fields read:
`id`, `object`, `amount`, `currency`, `created`, `charge`, `reason`, `status`,
`livemode`, `is_charge_refundable`, `metadata`, `evidence` (only which fields are
present, never their content), `evidence_details.due_by`, `.has_evidence`, `.past_due`,
`.submission_count`, `payment_method_details.type`, `.card.brand`, `.card.case_type`,
`.card.network`, `.card.network_reason_code`, and each of `balance_transactions[]`:
`id`, `object`, `amount`, `currency`, `created`, `fee`, `fee_details[]` (`amount`,
`currency`, `type`), `net`, `reporting_category`, `status`, `type`. Test mode only
unless `--allow-live`.
Or pull it from the API with a test-mode key: `STRIPE_API_KEY=sk_test_... scruple
ingest stripe-pull --since 2026-01-01 --until 2026-07-01 --out disputes.jsonl` (paged,
resumable, charge expanded, free text dropped; docs/INTEGRATIONS.md).

**Adyen** (`--format adyen`): the dispute notifications received (standard
notification envelopes, a JSON array of them, or JSON Lines), with the HMAC key
(`--adyen-hmac-key`, repeatable). Fields read: `live`, and per item `amount.currency`,
`amount.value`, `eventCode`, `eventDate`, `merchantAccountCode`, `merchantReference`,
`originalReference`, `pspReference`, `success`, `additionalData.hmacSignature`,
`chargebackReasonCode`, `chargebackSchemeCode`, `rfiReasonCode`, `rfiSchemeCode`,
`nofReasonCode`, `nofSchemeCode`, `defensePeriodEndsAt`, `disputeStatus`. Non-dispute
items are skipped and counted. Adyen notifications carry no balance postings, so costs
are read from the state.

**CSV or JSON Lines** (`--format csv --csv-config import.json`): an `ImportConfig`
(`scruple.ingest.tabular`) naming the columns for `external_id`, `payment_ref`,
`amount`, `currency`, `status`, `reason`, `opened_at` (required) and `merchant_ref`,
`network`, `network_reason_code`, `evidence_due_at` (optional), the amount format, and
`status_map` / `reason_map`. Row errors are counted on the report.

## Gating CI on the history

`scruple ci run --history <case store or export>` (docs/pilot/SCOPE.md week 5) runs the synthetic gate
and this backtest on the same candidate rule and shows both on the scorecard as
separate sources (`synthetic_verdict`, `history`). History verdict: `worse` is BLOCK;
`better` is PASS only if robust at both ends of the estimates, else HOLD; `insufficient
evidence` is HOLD; `no change` is PASS (nothing to regress). A history below `k` cases
cannot be calibrated: HOLD, estimates from the synthetic default profile, said so. The
overall verdict is the worse of the two sources. Same inputs, same scorecard bytes (the
history is identified by a content hash, not a path; tested, also with shuffled input).

## Privacy

- Free text is never kept: Stripe evidence is reduced to the names of the fields
  present; Adyen's `reason` text is dropped; metadata is read only for the synthetic
  marker. For anything else stored, use `scruple.security.redact` (PII placeholders),
  `scruple.security.pan` (keyed card tokens) and `scruple.security.crypto` (field
  encryption); see docs/INTEGRATIONS.md.
- Calibration sees aggregates only (above). The profile and the calibration report can
  leave the partner's environment; the backtest report holds per-reason totals, and
  per-case lines (case ids) only with `--include-cases`.
- The backtest itself runs where the data is. Nothing calls a network.

## Limits

- Selection bias: only cases the merchant chose to contest have observed contest
  outcomes. The win rate used for "contest where history accepted" comes from those
  cases and is probably optimistic for the rest. The estimate band covers sampling
  error, not this bias.
- Every counterfactual relies on the simulator's calibration and on the cost
  assumptions above. Results on estimated cases are model-based, not observed.
- Suppression is per table, not a formal privacy guarantee (no cross-table audit, no
  differential privacy).
- Stripe exports carry 3-D Secure, AVS/CVC and refund timing only with the charge
  expanded; no Stripe or Adyen dispute export carries MCC or delivery confirmation (CSV
  columns can). Rules that need a missing field see the default, and the report says
  how often.
- One currency per run; amounts for rules are converted with the simulator's synthetic
  FX table (USD, EUR, GBP only).
- The decision is placed at the open time (plus a fixed delay); the real decision
  times are not in the exports.
- Stripe accept and missed-deadline outcomes cannot be told apart.
