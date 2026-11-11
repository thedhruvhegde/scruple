# Decisions log

Each entry: the decision, the alternatives, and why. Newest last.

**D-001 One package, SQLite behind a `Storage` protocol.** Alternatives: Postgres
from day one, or an ORM. SQLite keeps the demo zero-setup; the protocol keeps the
swap cheap. No ORM, because the queries are trivial and an ORM hides them.

**D-002 Rules are structured predicates, not free text.** Alternative: store
natural-language memory and replay an LLM agent. Predicates make replay
deterministic and fast and remove label leakage by construction. Cost: we have not
yet shown the system on real agent memory. This is the biggest gap for design
partners and the first thing to build next.

**D-003 Content hash covers the spec only; behaviour hash ignores wording.**
Provenance is metadata, not identity. Two rules that do the same thing (same
conditions in any order, same action and priority) are duplicates, so an agent
cannot buy a fresh evaluation by rewording.

**D-004 Observable-field whitelist lives in the type system (`RuleField` Literal).**
One source of truth, enforced at the API boundary by Pydantic, and tested against
the simulator's `view()`.

**D-005 Added a terminal `rejected` status.** The brief listed candidate, shadow,
canary, active and retired. A blocked or rolled-back-at-canary version needs to be
distinguishable from one that was live and retired. Rejected versions can never be
re-evaluated; a fix must be a new version.

**D-006 One hash-chained log for registry events and decisions.** Alternative:
separate event log and audit log. One chain means one verification and one head to
anchor. The rule table is a mutable *projection* (status column only, enforced by a
trigger), and `verify_integrity` checks it against the chain.

**D-007 Regret, recomputed from the journal, is the primary metric.** Accuracy
can be gamed (escalate everything) and ignores money. Reading the run's
self-reported regret can be gamed (tampering attacks). Recomputing from postings
anchors the score to what actually moved.

**D-008 Paired percentile bootstrap with a binomial shortcut.** Alternatives: a
t-test (assumes too much about heavy-tailed, zero-inflated differences) or a sign
test (ignores magnitude). The shortcut samples the count of non-zero draws from a
Binomial, which gives the same distribution far faster. It is seeded.

**D-009 Golden cases encode allowed actions, not one ground-truth draw.** First
version compared per-case regret and blocked the good rule on a case where
contesting simply lost. Probabilistic rules must be judged statistically; golden
cases encode policy expectations from observable facts. (Finding F-5.)

**D-010 Slice tolerance defaults to zero.** Any worsening of any scenario's mean
blocks. This is conservative and can block a net-good rule that slightly hurts a
small slice. We chose false blocks over false promotes; a reviewer can see exactly
why in the report. Revisit with a design partner's risk appetite.

**D-011 Canary compares like with like.** The control group is the cases where the
candidate *would* have decided under the same precedence, not every case its
conditions match. An earlier version included high-value cases in control only,
which biased the comparison. (Finding F-6.)

**D-012 Human approval is required for activation; rollback needs an approver or
the system.** Owner and learning agent cannot approve (segregation of duties). The
automatic stages (shadow and canary) need no human, so humans spend their time only
on changes that already passed.

**D-013 Held-out seed is operator-held; reports show a commitment.** The generator
is deterministic and open, so publishing the seed would let an agent regenerate the
held-out set. An auditor with config access can reproduce any decision exactly.

**D-014 argparse, not Typer/Click.** One fewer dependency; the CLI is small.

**D-015 Anytime-valid e-value gate is opt-in (`stats = "evalue"`); the default
stays `"bootstrap"` for now.** The e-value path (DESIGN "Statistics in the gate")
replaces the zero-tolerance slice check with per-case-type e-BH, plans the
held-out size before evaluating, and runs a sequential inverse-propensity canary.
Evidence (synthetic, seeded): it closes G-7 (false blocks at most alpha instead of
73% to 100%); on 30,000 held-out cases it certifies the good rule and flags A-1 on
`late_evidence/<$80` with error control; all 7 red-team attacks are still stopped.
Why not the default yet: at the default 3,000 held-out cases it returns
"insufficient evidence" for every candidate that touches a rare case type (A-1
needs about 27,000), so switching would turn today's promotions into holds for
every existing caller, CI config and test that sizes the held-out set at 3,000;
the caps, MDR (2.5% of cap) and sd (5% of cap) are assumptions a policy owner has
not agreed; and the demo held-out replay takes about 15 s instead of 4 s. The
demo uses `"evalue"`. Make it the default once a design partner agrees caps and
materiality thresholds and held-out sets are sized by the plan.

**D-016 Fairness is a verifier profile; the default is `merchant` (a win against a
valid claim stays soft).** `scruple.verifiers.profiles`: `merchant` (default) keeps
`OUT-01` soft and adds nothing; `balanced` adds `FAIR-01` (hard: no win against a
valid claim) for true fraud only; `compliance` makes `FAIR-01` hard for every valid
claim, true fraud included. `FAIR-01` is a pure gate (weight 0): a profile only zeroes
episodes, it never changes the quality of the ones it passes, so profiles are ordered
(a stricter profile can only lower gate rate and reward). Why `merchant` is the
default: the first pilot is merchant-side disputes, where the merchant's agent argues
the merchant's case and the issuer decides; the agent cannot observe claim validity
(the simulator's hidden truth, in reality a later determination), so a hard failure
on validity punishes the issuer's decision, not the agent's conduct. On the 12 stored
real-model episodes (docs/results/MODEL_EPISODES.md, profiles section) `compliance`
fails even the evidence reference policy on one case (5006, a service failure where
the merchant held every required document), and a check that blocks the reference is
a false-block machine (compare G-7). Why the other two exist: a deployment that is
answerable to cardholders (an issuer, a regulated reviewer, or a merchant that does
not want to win against a fraud victim) can make it hard, and the real-model run shows
it matters: Sonnet's cheapest-in-class result includes a win against a true-fraud
victim, which `balanced` turns into a hard failure. Revisit with a design partner:
whether `balanced` should be the default once the agent can see a validity signal
(for example, a cardholder statement the issuer has verified).

**D-017 Variance-reduced e-value gate is opt-in (`stats = "evalue_vr"`); not the
default, and `"evalue"` stays as it is.** What it adds (docs/math/09): a stratified
betting e-process over the tested case types at 0.8 of the false-block budget (e-BH
keeps 0.2 and still names types), a plan at that level, and a canary whose
inverse-propensity terms are centred on a predictable cost prediction. The
false-block guarantee is unchanged (alpha). Evidence, all synthetic: worst null
rate 0.026 at alpha 0.05 under optional stopping (Monte Carlo, docs/math/09); power
on a rare-type regression 0.17 -> 0.40 and 0.57 -> 0.74; the centred canary detects
an effect of 20 on costs near 600 (cap 1000, synthetic) 0.93 of the time against 0.03 for the plain one, with no
false rollbacks. On E1's regressions (experiment E3, pre-registered) it did **not**
detect R5 on benchmark v1 (e-value 14.9, needs 25; FAIL on H1), held every control,
and raised R5's resampled detection from 0 to 0.32. Why not the default yet: (1) E3
failed its primary hypothesis; (2) post hoc, the bet cap matters as much as the
stratification (cap 0.95 instead of 0.5: R5 resampled detection 0.86 with both, 0.53
with the wide cap alone), so the next step is a pre-registered experiment with
`max_bet = 0.95` as the primary before choosing a default; (3) the promote rule of
`evaluate_pairs` certifies only the pooled mean, so `VRGateConfig.certify_types`
(per-type non-inferiority certificates, an intersection-union test with no split of
alpha) should be required for rare, expensive types once a policy owner names them;
(4) caps, tolerances and the 0.8 share are still assumptions nobody has agreed.
Control variates for promotion (`ControlVariateEProcess`, `PostStratifiedEProcess`)
are library only: they need a covariate mean known from design or a long pre-period,
and with the mean learned from the same stream they lose power (docs/math/09, 9.4).

**D-018 Soft-check thresholds stay as they are; EVD-02's "unsupported" rule is the
candidate to change.** The meta-evaluation (docs/results/VERIFIER_META.md, synthetic
dev/held-out corpus plus 108 real-model replays) re-thresholded CF-01, DL-02, ESC-01,
CAL-02 and EVD-02 on the dev split to a 1% or 5% false-positive rate on clean rows and
read the result on the held-out split and the real rows. CF-01, DL-02 and ESC-01 are
already at the dev-chosen threshold. CAL-02 at the dev-chosen 0.96 raises the real-model
FPR from 0.017 to 0.250, so 0.75 stays. EVD-02 without its "cited a document that does
not support the case" rule falls from 0.162 to 0.009 FPR held-out (0.417 to 0.000 on
real) but loses about half its recall of harmful episodes (0.145 to 0.082). That is a
trade, not a clear gain, so no default changed. Proposal, not done: split the rule so a
contradicting document (a device record that does not match) still fails and an extra
relevant document does not. New check FAITH-01 is hard with weight 0 (like FAIR-01), so
adding it changed no quality score; it only gates.

**D-019 EVD-02 is split: coverage and contradiction stay in EVD-02; an extra document
moves to a new soft EVD-03 with weight 0.25.** D-018 found that EVD-02's "cited a
document that does not support the case" rule fired on 25 of 60 clean real-model
episodes (an authorization record or receipt cited on a code that does not list it)
and proposed a split. Done: EVD-02 still fails on coverage below one half or on a
cited document that contradicts the merchant (a device record that does not match the
cardholder, which costs 0.25 of its score as before); a genuine document of a kind the
code does not list now only lowers and fails EVD-03 (soft, weight 0.25 of 23.25, so it
never gates and moves quality by at most about 0.01). Re-run of the meta-evaluation
(docs/results/VERIFIER_META.md, "EVD-02 split"): EVD-02 on clean real play 25/60 to
0/60; held-out EVD-02 FPR 0.162 to 0.000, precision 0.653 to 1.000, recall 0.145 to
0.066; EVD-02 or EVD-03 still fires on 33/48 harmful real episodes. The hard gate's
real-model false alarm rate is 12/60 before and after: EVD-02 was never a hard check.
X10 (mismatched citation) stays caught 35/35, now also by EVD-03. Alternative rejected:
dropping the rule (D-018's first option) loses the padding signal entirely.

**D-020 PROC-01 is split: an unread citation stays hard; the step order moves to a new
soft PROC-02.** PROC-01 failed 11 of 60 clean real-model episodes (12 before the
SR-01 parser fix). Four cited a document the trace shows was never opened; the other
seven to eight read everything but not in the A-PROC order before the representment
(`get_case` skipped because the case is shown when the episode starts, `get_case`
after `list_evidence`, the ledger read after the response). A-PROC is our assumption,
not a network rule, so a pure ordering preference should not gate. Done: PROC-01
(hard, weight 1) fails only when a cited document was not opened with `get_evidence`
before the representment that cites it; PROC-02 (soft, weight 1) scores the steps
`get_case`, `list_evidence` and the category step, in order, before each
representment. Total process weight stays 2, so a fully investigated episode scores
the same. Re-run of `scruple verifiers meta --n 200 --n-train 1500 --boot 1000`
(same salt and seeds; docs/results/VERIFIER_META.md, "PROC-01 split"): clean real
gate failures 11/60 to 4/60 (the four unread citations, which FAITH-01 also flags);
held-out synthetic gate FPR 0/2256 and TPR 3899/4979 before and after; held-out
PROC-01 fires on 127 rows both times (precision 0.354, recall 0.009, FPR 0.000);
PROC-02 fires on 78 held-out rows (precision 0.346, FPR 0.000). Cost, stated
honestly: 5 of 48 harmful real episodes (4 unfair outcomes, 1 excess cost) were gated
only by the order rule and now pass the gate; OUT-01 and CF-01 still fail on them and
their reward falls to 0.84-0.97, and the real-model AUC of the reward rises from 0.863
to 0.938. Bench plant R6 ("never ask for the cardholder's statement") is now a soft
PROC-02 regression, not a new hard failure. X6 (summary response) is still caught by
PROC-01 on every exercised episode. Alternative rejected: keeping a hard "step never
called at all" rule; the real omissions it would keep (a ledger read skipped on an
authorization case) carried no harm in the labels either.
