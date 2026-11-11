# Formal checks: what is proved about the money, and what is not

`scruple/formal/` holds abstract models of three engines (card disputes, ACH returns,
bank reconciliation) and checks money invariants on them with the z3 SMT solver:
bounded model checking to a depth k, plus an induction check that, where it succeeds,
extends the result to every depth. A conformance test steps each model next to the
real engine on random action sequences, so the model that is checked is the code that
runs. Everything here is about the synthetic simulators; it says nothing about real
networks, banks or processors.

Run it: `make formal` (or `uv run python -m scruple.formal.run [--depth N] [--json]`).
The slow suite runs the same (`tests/test_formal_slow.py`); the default suite runs the
same checks at a smaller depth plus the conformance tests (`tests/test_formal_*.py`).

## How it is built

- **One model, two backends** (`ops.py`). Each model is written once as guarded
  commands over a state dict: a guard (when a step may happen) and an update (the next
  state). The same functions run on Python ints (conformance) or build z3 terms (proofs).
  There is no second copy of the model to drift.
- **Bounded model checking** (`bmc.py`). Parameters (amounts, fees, days, windows, the
  case's shape) and per-step inputs (the issuer's award, a refund amount, a deadline)
  are symbolic: a result covers every value the constraints allow, not samples. At each
  depth j <= k, and for each invariant separately, z3 is asked for a path of j steps
  that breaks it. A state invariant proved at depth j is then assumed at state j (it
  holds on every reachable state there), which keeps deep checks cheap. Cover checks
  ask, for every command, whether some path within k fires it, so a proof cannot hold
  only because nothing happens.
- **Induction** (Houdini). Start from the invariants that hold initially; drop any that
  one step from a state satisfying all the others can break; repeat. What is left is
  jointly inductive: it holds on every reachable state at any depth (still under the
  abstraction). Each model has a `well_formed` invariant (the shape of a reachable
  state) that the others lean on; it is checked like the rest.
- **Counterexamples** come back as concrete traces (parameters, commands, inputs, every
  state) and replay on the Python model (`Counterexample.replay`), so they are runs of
  the model, not solver artefacts.

## The conformance tests: why the model cannot drift from the code

For each engine (`conformance.py`, `ach_conformance.py`, `recon_conformance.py`) a
seeded random agent tries every action kind, legal or not, with random amounts, ids
and evidence, and moves the clock. After every engine call:

1. The engine's event log is cut into steps where the engine itself starts one (an
   accepted action, a refund settling, a missed deadline, an issuer reply, a ruling;
   for ACH a return, dishonor, contest or retry settling). Each step names one model
   command. The only things read from the engine to choose it are the command's name
   and the facts the model leaves open: the issuer's award, whether a dishonor was
   timely, whether a retry found funds.
2. The command must be enabled in the model (its guard holds), and the model's next
   state must equal the abstraction of the engine replayed to the end of that step
   (`DisputeEngine.at`, `AchEngine.replay`): stage, who acts, resolution, dates,
   deadline, refunds in flight, what was returned, every ledger balance, fees,
   violations, incidents; for ACH every return slot and dishonor state; for
   reconciliation every match, adjustment, timing mark, escalation and the reported
   difference.
3. The engine's allowed actions equal the model's, and every refused action is one the
   model refuses too (except malformed ids, codes or kinds, which the abstraction leaves
   out).
4. Every invariant holds on every state and step the real engine reached.

Mutation tests check the conformance tests have teeth: a fee off by one, a deadline a
day late, a missing race check, a dropped reversal, a wrong contest rule or a looser
guard each make them fail. Coverage: on the default suite every dispute command and
every ACH command except the ones that cannot happen (below) is exercised.

FX is the one place the model has its own arithmetic: `fx.py` restates
`FxTable.convert` (28-digit `decimal` context, ROUND_HALF_EVEN, the product rounded to
28 significant digits and then to an integer) with fractions and two explicit rounding
functions. Hypothesis compares it with `FxTable.convert` for amounts up to 10^13 minor
units, every currency pair and two years of days; the dispute conformance compares it
again through every posting a foreign-currency case makes.

## What is proved

Depths, times and results below are from `make formal` on this machine (one run,
4 shared cores; times vary with load). "All depths" means the invariant is in the
inductive set; "to k" means bounded only.

| model | depth k | invariants | result | BMC time | induction time |
|---|---|---|---|---|---|
| dispute | 30 steps | 9 (incl. `well_formed`) | all hold to 30, all inductive (all depths) | 90.6 s | 0.1 s |
| ach | 12 steps | 8 (incl. `well_formed`) | all hold to 12, all inductive (all depths) | 29.9 s | 1.7 s |
| recon | 10 steps | 6 | all hold to 10, all inductive (all depths) | 5.0 s | 0.3 s |

Whole run: 2 min 8 s wall clock. Per-invariant solver time for the dispute model
ranges from 0.4 s (`terminal_absorbing`) to 21.5 s (`well_formed`). Cover checks: every
dispute and reconciliation command fires within the depth; for ACH all but
`DISHONOR_FINAL_R1..R3` (unreachable, see below) and `DISHONOR_CONTESTED_R3` (it needs
three retries and a dishonor, more than 12 steps). Before the fixes below,
`refund_race_safety` failed at depth 0 and the two ACH money invariants at depth 3
and 5; they are kept as regression tests.

### Card disputes (`dispute.py`, one case)

The model covers the lifecycle and both workflows (collaboration and allocation),
merchant deadlines, issuer and network timing, a refund in flight settling before or
after filing (races), agent refunds of any amount, partial disputes (disputed below
captured), split-order partial wins (the issuer may award any amount from zero to the
disputed amount at every decision), fees on entering each stage and on losing
arbitration, chargeback debits, reversals, violations and incidents.

- `double_entry`: every step's postings sum to zero; all balances net to zero.
- `conservation`: cardholder = returned - captured; merchant = captured - returned -
  fees; fee payees = fees. So cardholder + merchant + fees = 0 throughout.
- `no_double_refund`: a refund (agent or in flight) never returns more than the
  capture; returned exceeds the capture only through a network debit, and then the
  ledger has recorded a violation.
- `no_negative_balance`: fee payees and returned never below zero; the merchant below
  zero only by fees, the cardholder never above zero, unless a violation was recorded.
- `deadline_monotonicity`: event days never go backwards; every merchant deadline is
  after its stage started and is never pulled earlier; issuer and network answers are
  due after their stage started; no agent action is accepted after its deadline.
- `terminal_absorbing`: resolved exactly when the stage is resolved; afterwards stage,
  resolution, dates and fees never change, and money moves only by a refund already in
  flight (bounded by the other invariants).
- `refund_race_safety`: with no agent refund the cardholder never holds both the
  merchant's refunds and the chargeback without an incident flagging it, and a refund
  settling after a chargeback debit never takes the cardholder past the disputed
  amount.
- `no_crash`: no reversal above what was returned, no stage fired before it was
  entered (the engine would raise).

### ACH returns (`ach.py`, one entry)

- `double_entry`, `allocation`: collected + written off + voided + open receivable =
  the invoice, always, and revenue stays the invoice.
- `no_money_invented`: the originator never collects more than it invoiced.
- `no_negative_balance`: receivable never below zero, even after subtracting what
  dishonors and a retry in flight will collect; accepted, written off and fees never
  below zero.
- `dishonor_rules` (the return-window and dishonor/contest rules): a dishonor stands
  only against an improper return (R67 against a duplicate, R68 against an untimely
  return) and only if it settled in its window; otherwise it is contested (R72, R73,
  R75) and exactly the dishonored amount moves back; dishonor states only move forward;
  a return slot is never settled twice.
- `reinit_rules`: every retry the rules forbid (after a non-NSF return, past two, past
  the 180-day window, wrong description) is recorded as a violation; at most two
  retries are proper.
- `terminal_absorbing`: a resolved entry (quiet, nothing receivable) has no next step.

Never reachable, as the cover checks show: a dishonor of a retry's own return standing
(`DISHONOR_FINAL_R1..R3`). A retry's return settles on its last timely day and has no
duplicate, so any dishonor of it is contested.

### Reconciliation (`recon.py`)

- `double_entry`; `book_cash`: book cash = opening + every book line + every
  adjustment, nothing else posts.
- `tie_out`: the engine's difference = statement lines not escalated - book lines
  neither timing nor escalated - adjustments.
- `handled_at_most_once`: no line both matched and escalated (or marked timing), none
  in two match groups; match and adjustment ids are issued in order.
- `append_only`: adjustments, escalations and timing marks are never undone or changed.
- `no_money_invented`: when the reconciliation closes (difference zero, every line
  handled exactly once, every match balanced), the adjustments add up to exactly the
  statement lines they cite less the book lines they correct.

The engine does not judge adjustments; the recon verifier's REC-EVID check does. The
checker shows both by-design gaps reachable (`recon.witnesses`): a zero difference
reached with an adjustment that cites no statement line, and a statement line both
matched and cited by an adjustment. That is why `no_money_invented` is stated for a
closed, handled-once reconciliation and not for every state.

## Bugs found, and fixed

Each counterexample became a failing test on the real engine (committed as a strict
xfail), then a fix in its own `[!]` commit. All are in the synthetic simulators.

1. **A chargeback debit on top of a settled refund was not flagged**
   (`refund_race_safety`, depth 0). A partial dispute whose refund settled before
   filing, opening as a chargeback: the network-forced debit posts on top of the refund
   and the cardholder holds the disputed amount twice. Within the capture, so the
   ledger recorded no violation and the engine no incident. Reached by generated cases
   (FULL_MIX seed 3, case 105). Fix: `_on_chargeback` records a `double_refund`
   incident when the debit takes the cardholder above the disputed amount
   (`[!] flag chargeback debits that land on top of a refund`).
2. **A racing refund paid a partly won dispute twice** (`refund_race_safety`,
   depth 3). Represent, the issuer awards the merchant part of a split order and the
   engine reverses that part, then a refund in flight settles: the race check blocked a
   refund only while the chargeback still returned the whole dispute, so the
   cardholder kept the lost parcel's chargeback and got the refund too. Needs a valid
   case the generator does not draw (a partial refund, or undisputed parcels); built in
   `tests/test_formal_dispute_bmc.py`. Fix: once a chargeback was debited, a racing
   refund that would take the cardholder past the disputed amount is refused
   (`[!] stop racing refunds paying a partly won dispute twice`).
3. **A dishonor and a retry could both collect the same ACH entry**
   (`no_money_invented`, `no_negative_balance`, depth 3 to 5). After an R01 return the
   originator reinitiates and, before the retry settles, dishonors the return; both
   settle the next banking day, the receivable goes negative and the entry is collected
   twice. Generated cases show it for a day, until the RDFI's contest takes it back;
   with an untimely R01 and a receiver whose funds have arrived the dishonor stands and
   it stays collected twice. Two dishonors in flight on one returned amount do the
   same. Fix: a dishonor must find the returned amount still open after dishonors and a
   retry in flight (`AchEngine.in_flight`)
   (`[!] stop a dishonor collecting an entry a retry also collects`).

The fixes change behaviour only in those cases; the event-sourced replay is untouched
(the new incident is an ordinary `IncidentRecorded` event, the refusals change nothing).

## What is not proved

- **The abstraction.** Results are about the models. The conformance tests check the
  engines follow them on many random runs, which is evidence, not proof, that every
  engine run is a model run.
- **Bounds.** Dispute and ACH checks are per case or entry (cases are independent in
  the engine). ACH has five return slots: two scheduled returns per case and at most
  three retries (the engine allows more, recording violations). Reconciliation is
  proved at 2 statement lines, 2 book lines, 2 match groups and 2 adjustments
  (`CHECK_SIZE`); larger sizes made induction too slow to run routinely. Bounded results
  hold to the depth stated; inductive ones at every depth, at those sizes.
- **Time and days.** For disputes a deadline is any day after its stage started, which
  covers every network window and calendar but does not check the windows themselves
  (`docs/RULES.md` does, against the rules data). ACH time is abstract: anything
  pending may settle in any order (a superset of date order); timeliness of returns
  and dishonors enters as data.
- **The issuer, the RDFI and the receiver** are nondeterministic: any award, contest
  outcome (within the rules above) or funds. Nothing here says how often anything
  happens.
- **FX.** The z3 checks are single-currency (postings in the transaction currency).
  FX rounding is checked by the conformance and hypothesis tests against the integer
  model, not proved: z3 could not decide the 28-digit rounding queries in minutes. In
  200,000 sampled conversions (amounts up to 10^12) the 28-digit context never rounded
  differently from rounding the exact value once; that is a sample, not a proof.
- **Not modelled:** fines from monitoring programmes, evidence content and forgery
  detection, request_info (a no-op), more than one case per engine, the ACH ledger's
  parsing of statements, and everything outside `scruple/sim` (verifiers, gate, API).
