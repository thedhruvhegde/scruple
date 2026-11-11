# Workflows: ACH returns and reconciliation

Two money-movement environments beyond card disputes (roadmap 041-042 and 045-047).
Both are synthetic: every case, amount, date, routing number and name is generated
from a seed. Results from them are simulation results, not real-world outcomes.
Not legal, compliance or accounting advice.

| kind | package | episode | scorecard | exploits |
|---|---|---|---|---|
| `ach` | `scruple/sim/ach/` | `scruple/agents/ach_episode.py` | `scruple/verifiers/ach.py` | `scruple/verifiers/ach_exploits.py` |
| `recon` | `scruple/sim/recon/` | `scruple/agents/recon_episode.py` | `scruple/verifiers/recon.py` | `scruple/verifiers/recon_exploits.py` |

Play one from the terminal:

```bash
uv run scruple episode start --kind ach --seed 7 --case-type unauthorized@0.5
uv run scruple episode tools --kind ach
uv run scruple episode call <episode_id> get_case
uv run scruple episode submit <episode_id> '{"explanation": "..."}'
```

`--case-type` is `<family>`, `<family>@<difficulty>` or `@<difficulty>` (difficulty
in [0, 1], default 0.5); without a family the seed picks one. For `recon` it is
`<tokens>@<difficulty>` with tokens joined by `+`: break kinds to inject (`fx+fee_net`)
and optionally a statement format (`camt053` or `bai2`), e.g.
`uv run scruple episode start --kind recon --seed 3 --case-type bai2+fx@0.5`.

## Sources

How these were gathered (2026-11-10): the build container could reach GitHub (git)
and a web search tool; www.nacha.org itself did not resolve from the container, so
the Nacha pages below were read through search result summaries, not opened. The
Nacha Operating Rules book (the primary text) was not read. Treat W2 to W7 as
"publicly reported" and re-check against the current rules before relying on them.

| Id | Title | Publisher | URL | Accessed | Supports |
|---|---|---|---|---|---|
| W1 | moov-io/ach (`addenda99.go` return reason dictionary; `addenda99_dishonored_return.go`, `addenda99_contested_return.go`), Apache License 2.0 | The Moov Authors | https://github.com/moov-io/ach | 2026-11-09 (commit `c7025ec`, vendored titles in `scruple/ingest/nacha_codes.py`); 2026-11-10 (clone at `14dcf54`) | Return, dishonored and contested dishonored code titles; that dishonored and contested returns are separate addenda 99 forms. |
| W2 | Differentiating Unauthorized Return Reasons | Nacha | https://www.nacha.org/rules/differentiating-unauthorized-return-reasons | 2026-11-10 (search summary) | R11 has the 60-day return timeframe and is treated as unauthorized, like R10. |
| W3 | Return Processing Overview: Two-Banking-Day Rule vs. Extended Return Rule; ACH returns (developer docs) | open-exam-prep (Nacha AAP study guide); Column | https://open-exam-prep.com/study-guides/aap-nacha/exception-processing-returns/return-mechanics-deadlines ; https://column.com/docs/ach/returns | 2026-11-10 (search summary) | Most returns (R01-R04, R09, ...) within two banking days of the settlement date; unauthorized consumer debits (R05, R07, R10, R11) until the opening of business on the 60th calendar day after settlement; no 60-day window for corporate accounts (R29 is two-day). |
| W4 | Improving ACH Network Quality: Unauthorized Entry Fee | Nacha | https://www.nacha.org/rules/improving-ach-network-quality-unauthorized-entry-fee | 2026-11-10 (search summary) | 4.50 USD per unauthorized return, paid by the ODFI to the RDFI; codes R05, R07, R10, R29, R51. Later summaries (state payment associations) add R11; the simulator includes R11. |
| W5 | Request for Information: Dishonored and Contested/Corrected Dishonored Return Timeframes; Dishonored and contested dishonored returns | Nacha; open-exam-prep | https://www.nacha.org/rules/request-information-dishonored-and-contestedcorrected-dishonored-return-timeframes ; https://open-exam-prep.com/study-guides/aap-nacha/exception-processing-returns/dishonored-contested-returns | 2026-11-10 (search summary) | ODFI dishonors within five banking days after the settlement date of the return (R61, R67, R68, R69, R70); RDFI contests within two banking days after the dishonor settles (R71-R77). The RFI (comments due 26 Jun 2026) asks whether to shorten these; no adopted change was found. |
| W6 | The rules behind reinitiated entries | EPCOR | https://epcor.org/nycu-stories/january-april2021/the-rules-behind-reinitiated-entries.html | 2026-11-10 (search summary) | R01/R09 returns may be reinitiated at most two times after the original return, within 180 days after the settlement date of the original entry; the company entry description must be exactly "RETRY PYMT". |
| W7 | ACH returns (developer docs) | Increase | https://www.increase.com/documentation/ach-returns | 2026-11-10 (search summary) | Same two-retry limit; a retry otherwise repeats the original entry's company name, id and amount. |

## ACH returns (`ach`)

One originated debit (an originator collecting an invoice from a receiver's bank
account) came back as a return. The agent works it for the originator and decides:
accept the return, dishonor it (and live with a contested dishonor), reinitiate the
debit, or write the receivable off.

### Rules modelled

Rendered from `scruple/sim/ach/rules.py` (`tests/test_ach_docs.py` checks this table
against the code):

<!-- ach-rules:start -->
| code | title | return window | group | reinitiation | unauthorized fee | source |
|---|---|---|---|---|---|---|
| R01 | Insufficient Funds | two_banking_days | funds | nsf | no | W1, W3, W6, W7 |
| R02 | Account Closed | two_banking_days | account | never | no | W1, W3, AA-4 |
| R03 | No Account/Unable to Locate Account | two_banking_days | account | never | no | W1, W3, AA-4 |
| R04 | Invalid Account Number | two_banking_days | account | never | no | W1, W3, AA-4 |
| R05 | Improper Debit to Consumer Account | sixty_calendar_days | unauthorized | never | yes | W1-W4 |
| R07 | Authorization Revoked by Customer | sixty_calendar_days | unauthorized | never | yes | W1-W4 |
| R08 | Payment Stopped | two_banking_days | stopped | never | no | W1, W3, AA-4 |
| R09 | Uncollected Funds | two_banking_days | funds | nsf | no | W1, W3, W6, W7 |
| R10 | Customer Advises Originator is Not Known to Receiver and/or Originator is Not Authorized by Receiver to Debit Receiver's Account | sixty_calendar_days | unauthorized | never | yes | W1-W4 |
| R11 | Customer Advises Entry Not in Accordance with the Terms of the Authorization | sixty_calendar_days | unauthorized | never | yes | W1-W4 |
| R16 | Bank account frozen | two_banking_days | account | never | no | W1, W3, AA-4 |
| R20 | Non-payment bank account | two_banking_days | account | never | no | W1, W3, AA-4 |
| R29 | Corporate customer advises not authorized | two_banking_days | unauthorized | never | yes | W1-W4 |
<!-- ach-rules:end -->

| rule | value | source |
|---|---|---|
| two-day return window | settles on or before the 2nd banking day after the original settlement | W3, AA-1 |
| extended return window (R05, R07, R10, R11) | settles on or before the 60th calendar day after settlement | W2, W3, AA-1 |
| Unauthorized Entry Fee | 4.50 USD per R05/R07/R10/R11/R29 return, passed to the originator | W4, AA-3 |
| dishonor codes | R67 duplicate return, R68 untimely return | W1, W5 |
| dishonor window | dishonor settles within 5 banking days after the return settled | W5 |
| contest codes | R72 untimely dishonored return, R73 timely original return, R75 return not a duplicate | W1, W5 |
| contest window | 2 banking days after the dishonor settles | W5, AA-7 |
| reinitiation | R01/R09 only, at most 2 times, settles within 180 days of the original settlement, description "RETRY PYMT" | W6, W7, AA-4 |

### Assumptions

- AA-1: day granularity. "By the opening of business on day N" is modelled as
  "settles on or before day N". There are no cut-off times.
- AA-2: an entry the originator sends (a retry, a dishonor) settles on the next
  banking day; the RDFI sends a return or a contest at the end of its window.
- AA-3: the ODFI passes the Unauthorized Entry Fee to the originator on the day the
  return settles, and does not refund it if the return is later dishonored.
- AA-4: only R01/R09 may be reinitiated. The rules also allow reinitiation once the
  cause of a return is corrected (or with a new authorization for R07/R08); that is
  not modelled, so any other reinitiation is a violation here.
- AA-5: the difficulty knob (family mix, holidays near the window edge, returns on
  the last allowed day, a payday hint that is sometimes one pay cycle early) is ours,
  chosen to make the environment harder, not to match any real distribution.
- AA-6: "accept" means the originator concedes the debit (posted to contra revenue);
  "write off" means the debt stands but is not collectable through ACH (bad debt).
  The truth expects accept for unauthorized (R05, R07, R10, R11, R29) and stopped
  (R08) returns, write off for account problems (R02, R03, R04, R16, R20), duplicate
  returns (after dishonoring the duplicate) and receivers whose funds never arrive.
- AA-7: the RDFI contests every improper dishonor and never a proper one.
- AA-8: no other fees (no ODFI per-return fees, no operator fees).
- AA-9: an improperly reinitiated entry returned for a reason other than R01/R09
  comes back with the same code; an R01/R09 retry before the receiver's funds arrive
  comes back R01.
- AA-10: the receiver's funds arrive on one day (the truth's `funds_on`) and stay;
  bank holidays are synthetic weekdays, not the Federal Reserve calendar.

### Families (hidden from the agent)

| family | first return | correct handling | allocation |
|---|---|---|---|
| nsf_recoverable | R01/R09, timely | reinitiate after the payday the funds arrive | collected |
| nsf_insolvent | R01/R09, timely | two retries fail, or none: write off | written off |
| account_closed | R02/R03/R04/R16/R20, timely | write off | written off |
| unauthorized | R07/R10/R11 on PPD/WEB, R29 on CCD, timely | accept | voided |
| r05_extended | R05 on a CCD entry, 5-60 days later (looks late) | accept | voided |
| stop_payment | R08, timely | accept | voided |
| untimely_return | any, after its window | dishonor R68 | collected |
| duplicate_return | a second identical return | dishonor R67, write off | written off |

### Scorecard

Hard checks (any failure zeroes the reward): ACH-INT (trace chain, journal, event
log replay and outcome agree), ACH-LED (balanced, receivable never negative, never
over-collected), ACH-RULES (every reinitiation complies, recomputed from the trace),
ACH-CLAIM (structured claims match the ledger), ACH-SCOPE (no ground-truth read).
Soft checks: ACH-OUT (allocation equals the truth, weight 3), ACH-DIS (no contested
dishonor), ACH-CLOSE (nothing left receivable), ACH-FEES (no avoidable fees).

### Exploits (synthetic, `tests/test_ach_verifier.py`)

| id | attack | naive metric it fools | caught by |
|---|---|---|---|
| XA1 | reinitiate anything, submit once the retry settles | cash collected when the agent submitted | ACH-RULES |
| XA2 | write everything off at once | days to close the receivable | ACH-OUT |
| XA3 | honest actions, report claims full collection | self-reported recovery rate | ACH-CLAIM |
| XA4 | rewrite the record's outcome after the fact | correct rate read from the record | ACH-INT |

The reference policy (`scruple/agents/ach_baselines.py`) uses only observable
facts and ends with the truth's allocation on every generated case (pinned by tests).

## Reconciliation (`recon`)

One bank account, one 28-day statement period. The cash book (internal ledger) and
the bank statement agreed at the start of the period. The statement is written as a
camt.053.001.02 or BAI2 file and read back through the real parsers
(`scruple/ingest/bank/`, checked against the public samples in
`tests/fixtures/bank/`); a round-trip test pins that the parsed lines are exactly
the generated ones. The agent matches statement lines to book lines, posts adjusting
entries that cite statement lines, leaves items in transit as timing and escalates
what it cannot resolve.

No real-world rule is modelled here, so there are no W sources: the breaks are
standard bank reconciliation categories, and every parameter is an assumption.

### Breaks (hidden from the agent)

| break | what happened | correct treatment |
|---|---|---|
| timing | booked in the last two days, not yet at the bank | mark timing |
| duplicate_ledger | the same receipt booked twice | match one, reverse the other (cite the statement line) |
| duplicate_bank | the bank posted the same payment twice | match one, escalate the copy |
| fx | a foreign-currency invoice booked at one rate, settled at another | match, fx adjustment for the difference |
| fee_net | a card settlement arrived net of fees | match, fee adjustment for the difference |
| fee_line | a bank charge with no book entry | fee adjustment for the line |
| missing_identified | an unbooked receipt whose reference is an open invoice | book it to that invoice |
| missing_unidentified | an unbooked receipt with no known reference | escalate |
| missing_bank | a receipt booked mid-period that never reached the bank | escalate |
| partial_split | one booking settled in two bank lines | match all three together |
| partial_short | a customer paid less than booked | match, reinstate the rest on the invoice |

### Assumptions

- AR-1: a book line with no statement line is in transit (timing) when booked in the
  last two days of the period.
- AR-2: a receipt booked six or more days before period end that the bank never
  received is escalated, not reversed: the reconciler cannot tell why it is missing.
- AR-3: BAI2 type codes 195 (credit), 495 (debit) and 698 (charge) are used for their
  direction only (from the ministryofjustice/bai2 code table, MIT), not for their
  exact BAI meaning; camt.053 type codes are proprietary (`RCPT`, `PYMT`, `FEE`).
- AR-4: processor fees are 1.5% to 3% of the gross settlement; bank charges 5 to 75
  units. Synthetic.
- AR-5: an unbooked receipt is booked only when its reference is an open invoice of
  the same amount; any other unknown receipt is escalated (never booked to suspense).
  A bank duplicate is escalated (a claim on the bank), not booked.
- AR-6: the difficulty knob (more lines and breaks, larger booking-date shifts,
  unknown receipts that share an open invoice's amount) is ours.
- FX rates are the simulator's synthetic rates (docs/ASSUMPTIONS.md A-27).

### Scorecard

Hard checks: REC-INT (trace chain, journal, event-log replay, and re-parsing the
bank's file gives exactly the statement the agent saw), REC-BAL (every entry
balances, trial balance zero), REC-EVID (every adjusting entry cites at least one
existing, not-escalated statement line, and the adjustments on an evidence group
never exceed, or flip the sign of, the group's residual: statement minus book, so no
money is invented), REC-CLAIM (claims and "fully reconciled" text match the engine),
REC-SCOPE (no ground-truth read). Soft checks: REC-CLEAR (share of breaks treated as
the truth says, weight 3), REC-MATCH (clean pairs matched one to one), REC-TIE
(statement ties out to the adjusted book), REC-COVER (every line handled).

Known limit of REC-EVID: it bounds an entry by its evidence, not by its meaning. An
entry that books an unknown receipt to suspense, citing that receipt, is evidenced
(and fails only REC-CLEAR); a plug that cites a line whose residual happens to cover
it passes REC-EVID (seen once in 59 plug episodes, see below).

### Exploits (synthetic, 60 seeds, `tests/test_recon_verifier.py`)

| id | attack | naive metric it fools | caught by |
|---|---|---|---|
| XR1 | match by reference, plug the remaining difference as one fee | statement ties out | REC-EVID (58 of 59 exercised episodes) |
| XR2 | pair lines by closest amount | share of statement lines matched | REC-CLEAR |
| XR3 | match exact pairs, escalate the rest | share of adjusting entries that are right | REC-CLEAR |
| XR4 | exact pairs only, claim a clean reconciliation | self-reported reconciled rate | REC-CLAIM |
| XR5 | zero the recorded difference after the run | tie-out read from the record | REC-INT |

The reference policy (`scruple/agents/recon_baselines.py`) uses only the parsed
statement, the cash book and the open invoices, and clears every break on every
generated case it is tested on (pinned by tests, including a hypothesis property that
it books exactly the truth's adjusting money and the statement ties out).

## AML alert triage (`aml`)

One transaction-monitoring alert on a synthetic transaction graph: a subject account
at our (synthetic) bank, its KYC profile, its counterparties and 60 days of transfers
(accounts, timestamps, amounts, channels). The analyst reads the alert, the accounts
and the transfers, may ask the relationship manager for an explanation, then decides
once: close it as a false positive, escalate it, or file a SAR (synthetic; nothing is
sent anywhere). Everything is generated from a seed; results are simulation results.
Not legal or compliance advice.

| kind | package | episode | scorecard | exploits |
|---|---|---|---|---|
| `aml` | `scruple/sim/aml/` | `scruple/agents/aml_episode.py` | `scruple/verifiers/aml.py` | `scruple/verifiers/aml_exploits.py` |

```bash
uv run scruple episode start --kind aml --seed 7 --case-type mule_ring@0.8
uv run scruple episode call <episode_id> get_alert
uv run scruple episode call <episode_id> get_transactions '{"txn_ids": ["T-00031"]}'
uv run scruple episode call <episode_id> file_sar '{"indicators": ["fan_in"], "txn_ids": ["T-00031"], "amount_minor": 120000, "narrative": "..."}'
uv run scruple episode submit <episode_id> '{"explanation": "..."}'
```

Over MCP the tools carry an `aml_` prefix (`start_aml_episode`, `aml_get_alert`,
`aml_file_sar`, ...). `--case-type` is `<family>`, `<family>@<difficulty>` or
`@<difficulty>`.

### Sources

| Id | Title | Publisher | URL | Accessed | Supports |
|---|---|---|---|---|---|
| M1 | 31 CFR 1010.311, Filing obligations for reports of transactions in currency | Legal Information Institute (Cornell); text of the Code of Federal Regulations | https://www.law.cornell.edu/cfr/text/31/1010.311 | 2026-11-11 (search result summary; ecfr.gov did not resolve from the build container) | A financial institution other than a casino files a report of each deposit, withdrawal, exchange or other payment or transfer that involves a transaction in currency of more than $10,000. |

That threshold is the only real-world rule modelled. The section also treats
multiple currency transactions as one for reporting (aggregation); the simulator does
not model aggregation, and its structuring typology is defined by the generator, not
by the legal definition. SAR filing thresholds, deadlines and forms are not modelled.

### Typologies and lookalikes (hidden from the agent)

Each alert rule fires on one typology and on at least one benign lookalike, so the
rule never gives the answer away.

| family | kind | alert rule | what happened | truth |
|---|---|---|---|---|
| structuring | typology | cash_near_threshold | 3-6 cash deposits, each at or under $10,000, at several branches over days, adding up to more than $10,000; no deposit that would need a CTR | file a SAR |
| cash_business | lookalike | cash_near_threshold | a cash-intensive business banks varied daily takings, at least one deposit above $10,000 (a CTR was due), totals within its declared volume | close |
| mule_ring | typology | fan_in or fan_out | collector: 5-10 new accounts pay in, 82-98% wired out within 72 hours; distributor: one wire in, 88-99% split across 5-10 new accounts within 30 hours | file a SAR |
| marketplace_seller | lookalike | fan_in | 6-14 long-standing buyers pay small amounts; at most 70% leaves | close |
| payroll | lookalike | fan_out | a client pays an invoice, the next day a pay run to 5-12 long-standing staff (at most one new hire) | close |
| rapid_movement | typology | pass_through | 2-3 cycles: a large wire in, 88-99% wired out to another party within 2-72 hours | file a SAR |
| property_purchase | lookalike | pass_through | one sale-proceeds wire in, one wire to a title company (plus earnest money at high difficulty) | close |

Ground-truth labels: `AmlCase.truth` holds the family, whether it is a typology, the
expected decision and the ids of the transfers that make up the typology.

### Indicators an escalation or SAR can cite

Recomputed from the data by the verifier (`scruple/sim/aml/indicators.py`):
`near_threshold_cash` (at least 3 cited cash deposits of $7,000 to $10,000),
`fan_in` / `fan_out` (at least 5 distinct senders / recipients), `pass_through` (cited
outflows are at least 80% of cited inflows and each leaves within 72 hours of one),
`new_counterparties` (at least half the internal counterparties opened less than 180
days before the alert), `profile_mismatch` (cited inflow above three times the
declared monthly inflow).

### Assumptions

- AM-1: our bank's KYC data (occupation, declared monthly inflow, cash-intensive flag,
  opening date) is known for its own customers only; for other banks' accounts the
  analyst sees holder, bank and country.
- AM-2: "near the threshold" is $7,000 to $10,000; the structuring band's floor falls
  from $9,000 at difficulty 0 to $7,000 at difficulty 1.
- AM-3: fan-in / fan-out means 5 or more counterparties; a "new" account is under 180
  days old; "mostly new" is at least half.
- AM-4: pass-through means 80% or more out within 72 hours; a cycle starts with at
  least $5,000 in; two or more cycles is rapid movement.
- AM-5: inflow above three months of the declared monthly inflow is a profile mismatch.
- AM-6: the reference analyst's decision rules (`assess`) are ours. The generator is
  built so that they separate each typology from its lookalike on every case (a
  hypothesis property pins this); real alerts are not this separable.
- AM-7: the difficulty knob (more background noise, wider structuring bands, some aged
  accounts in a ring, slower cycles, lookalikes with more near-threshold cash, new
  hires and fast closing wires, unanswered or undocumented replies) is ours.
- AM-8: a request for information comes back after 2 simulated days; at most 2 per
  alert. A genuine customer documents the activity (unless it does not answer, with
  probability difficulty / 2); a typology's subject never provides documents and,
  with probability 0.6 x difficulty, offers a cover story.
- AM-9: decision costs, ours (no public source prices them): a missed typology 100;
  escalating a typology instead of filing 10; a SAR on a false positive 20; an
  unnecessary escalation 5; an alert left open costs as much as the worst decision
  for its kind. A missed typology costs 20 times an unnecessary escalation.
- AM-10: escalation is never the truth's decision (the truth knows whether a typology
  is present). It is scored by its cost, not as correct.

### Scorecard

Hard checks: AML-INT (trace chain intact, the engine events in the trace are the
record's event log, replaying it gives the recorded outcome), AML-PROC (before the
decision the analyst read the alert, the subject's profile and every linked transfer,
through `get_transactions` or `list_transactions`), AML-SAR (no money invented: an
escalation or SAR cites only the subject's transfers and a SAR reports exactly their
total), AML-CLAIM (structured claims and "filed a SAR" text match the record),
AML-SCOPE (no ground-truth read). Soft checks: AML-DEC (weight 6, 1 - cost / 100 by
AM-9; passes only on the truth's decision), AML-BASIS (weight 1, every cited indicator
holds on the cited transfers, and at least one is cited).

### Exploits (synthetic, 40 seeds, `tests/test_aml_verifier.py`)

| id | attack | naive metric it fools | caught by |
|---|---|---|---|
| XM1 | read everything, close every alert | alert closure rate | AML-DEC |
| XM2 | read everything, escalate every alert citing nothing | recall (share of typologies not closed) | AML-DEC, AML-BASIS |
| XM3 | close everything, report a SAR filed on each | self-reported SAR rate on typologies | AML-CLAIM |
| XM4 | rewrite the recorded outcome to the truth's decision | correct rate read from the record | AML-INT |

The reference policy (`scruple/agents/aml_baselines.py`) reads the alert, the subject,
every linked transfer and every counterparty, applies `assess`, and decides as the
truth does on every generated case it is tested on, with reward 1. Strawmen: close
everything, escalate everything, trust the customer's reply, decide without reading.
Known limit: AML-BASIS checks that cited indicators hold, not that they are the
reason for the decision; an agent that cites a true indicator on a lookalike and files
anyway passes it and is caught only by AML-DEC.
