# agent-gate design

## What it is

Change control for what agents learn. A learned rule is a *candidate* until it has
been replayed against held-out history, scored by verifiers that are hard to game,
run on a canary slice of live traffic, and approved by a named human. Every step is
recorded in a tamper-evident log and explained in an evidence report a model-risk
reviewer can read.

## Flow

```
agent proposes rule ──► registry (candidate, content-hashed, append-only history)
                            │
                            ▼
                     ┌── shadow replay ───────────────────────────────┐
                     │ held-out cases (operator-held seed)            │
                     │ baseline policy vs baseline+candidate, paired  │
                     │ verifiers: ledger · outcome · policy           │
                     │ paired bootstrap CI · per-slice · golden suite │
                     │ escalation guard · conflict/override analysis  │
                     └──────────┬──────────────┬─────────────┬────────┘
                         BLOCK (rejected)  HOLD (candidate)  PROMOTE
                                                               ▼
                     ┌── canary ──────────────────────────────────────┐
                     │ deterministic hash split of "live" traffic     │
                     │ realized cost treatment vs control, hard checks│
                     └──────────┬──────────────┬─────────────┬────────┘
                       ROLLBACK (rejected)  HOLD (canary)  CANARY_PASSED
                                                               ▼
                         approver (not owner, not agent) ──► ACTIVE
                                                               │
            continuous assurance (golden + fresh replay) ──► auto-rollback
            manual rollback ──► previous version restored via rollback pointer
```

Every arrow is an audit-log entry. Every gate stage produces a `Decision` with a
markdown report.

## Modules

| Package | Responsibility |
|---|---|
| `agent_gate/domain.py` | The rule language: `Condition`, `RuleSpec`, `Action`. Whitelist of observable fields. |
| `agent_gate/registry` | `RuleVersion`, status state machine (`state.py`, hardcoded), `Registry` service: register, transition, activate, rollback. |
| `agent_gate/storage.py` | `Storage` protocol plus `SQLiteStorage`. Triggers make history append-only. Re-entrant transactions. |
| `agent_gate/sim` | Seeded case generator (8 scenario types), double-entry ledger, chargeback lifecycle (inquiry, chargeback, representment, arbitration, resolved), snapshot/restore, synthetic rule library. |
| `agent_gate/verifiers` | `ledger` (journal invariants plus reconciliation), `outcome` (regret from journal and ground truth), `policy_constraints` (per-case hard rules), `adversarial` (attack library and naive strawmen). |
| `agent_gate/gate` | `engine` (pure shadow and canary evaluation returning a `Verdict`), `stats` (seeded paired bootstrap), `provider` (`StatsProvider`: bootstrap or e-value), `evalue` (per-case-type e-BH gate and sample-size plan), `sequential` (anytime-valid two-sample canary), `regression` (golden suite), `redteam` (runs attacks), `config`. |
| `agent_gate/audit` | `log` (hash chain, verify), `report` (markdown evidence reports). |
| `agent_gate/service.py` | `AgentGate`: the only place verdicts change state. Used by API and CLI. Integrity verification. |
| `agent_gate/api` | FastAPI. Pydantic-validated inputs; `X-Actor` identity header (authn stub). |
| `agent_gate/cli` | argparse CLI, `demo`. |

Dependency direction: `domain` ← `registry`/`sim` ← `verifiers` ← `gate` ← `service` ← `api`/`cli`.
The gate engine is pure (no I/O), which makes it easy to test and to reason about.

## Key ideas

- **Rules are data, not code.** Structured predicates over whitelisted observable
  fields. Ground truth, scenario labels and identifiers are not addressable, so
  label leakage and per-ID memorisation are impossible by construction.
- **Paired replay.** Both policies see the same cases from the same ledger
  snapshot, so differences are attributable to the rule alone.
- **Regret, not accuracy.** The primary metric is money lost relative to the
  hindsight-optimal action, recomputed from journal postings. Accuracy is reported
  but never decides anything.
- **Averages lie.** Every scenario slice must not get worse, there are zero-tolerance
  constraint checks per case, and a golden suite encodes policy-owner expectations.
- **The gate never trusts the agent.** Self-reported metrics are stored and ignored.
  The held-out seed is operator-held, and reports show only a commitment. Identical
  or merely reworded rules cannot be resubmitted.
- **Determinism.** All randomness comes from explicit seeds. Clocks are injectable.
  The demo output is byte-stable (tested).

## Statistics in the gate (`GateConfig.stats`)

Two paths. Hard checks that do not depend on statistics (new ledger or policy
violations, golden suite, escalation guard, canary hard violations) are identical on
both. All numbers below are synthetic.

**`"bootstrap"` (default).** Seeded paired percentile bootstrap (D-008), a
zero-tolerance per-scenario slice check (D-010) and a point-estimate canary rule.
No error control: at the null boundary the slice check false-blocks 73% to 100% of
the time (gap G-7, docs/math/02), and the intervals are only valid at a fixed n.

**`"evalue"`.** Anytime-valid statistics from `scruple/stats/` (docs/math/):

1. *Case types and caps.* A case type is `scenario/amount band` (default bands
   `<$80`, `<$10,000`, `>=$10,000`). Each band has a declared cap on how far a
   change can move one case's cost: band top plus every fee (ASSUMPTION, from the
   synthetic fee schedule). Caps use observable amounts only; a case that breaks
   its cap fails the gate closed (`evalue_declared_caps`).
2. *Family.* Only types where the candidate changes a decision are tested. That is
   a function of observable fields, so it is fixed before outcomes. On cases whose
   decision did not change the loss must not change; the engine checks this and
   fails closed.
3. *Plan first* (`evalue_sample_size`). `scruple.stats.plan` (Bernstein bound,
   level `alpha/K`, power `1 - beta`) gives the cases each tested type needs to flag
   a regression of `tolerance + MDR` (MDR default 2.5% of the type's cap) assuming
   a per-case sd of at most 5% of the cap (ASSUMPTION; the observed sd is reported
   by `evalue_planning_assumption`, never used to decide). Too few cases: HOLD,
   "Insufficient evidence", with the required n per type and in total. A small
   held-out set never passes silently.
4. *Regression* (`evalue_no_type_regression`, hard). One betting e-process per type
   for `mean_k <= tolerance_k`; e-BH over the family at `evalue_alpha`. When no type
   regressed, P(false block) <= alpha. Replaces the zero-tolerance slice check and
   the bootstrap "not significantly worse" check.
5. *Improvement* (`evalue_improvement_certified`, evidence). One betting e-process
   per type for `mean_k >= 0`; e-BH at `evalue_alpha_certify`. Promotion needs at
   least one certified type. Replaces "significant improvement".
6. *Canary* (`canary_sequential_no_harm`). Arms are matched groups, not paired
   cases: a live case is decided by one policy only, so the paired test does not
   apply. Within the matched cases assignment has a known probability `p`, so the
   inverse-propensity term `Y (T/p - (1-T)/(1-p))` is unbiased for the per-case
   effect and bounded; a betting e-process per type on it, with e-Bonferroni on
   running maxima, is valid under continuous monitoring. It stops at the first
   crossing: harm (rollback) or benefit (early pass). P(false rollback) <= alpha.
   Ramp stages spend `alpha / stages` each. ASSUMPTION: hash assignment behaves as
   an independent Bernoulli(`p`) draw. If neither boundary is crossed the canary
   passes on "no evidence of harm", the same standard as the bootstrap path.
   Power is low at a 10% canary (variance scales with `1/p`); the shadow
   certificate is the primary evidence.

What the guarantees do not cover: distribution shift between held-out and live
traffic (G-2), shared simulator assumptions (G-4), delayed outcomes in canary, and
power when the sd assumption is wrong (reported, not enforced).

Evidence: the decision stores `shadow.evalue` (per-type e-values, confidence
sequences, required n, alpha share) and `canary.sequential`. Both are omitted from
the record when absent, so bootstrap decisions hash exactly as before.

Demo (`make demo`, 30,000 held-out cases, about 15 s): the good rule is certified on
both `not_received` types and promoted; A-1 is flagged on `late_evidence/<$80`
(e-value about 2e4 against a threshold of 80) and blocked. At 3,000 cases both
rules are "insufficient evidence" (A-1 is still blocked by its policy and golden
checks). `scruple demo --stats bootstrap` runs the original story.

## Audit threat model

| Threat | Control |
|---|---|
| Edit through the application | No update/delete API for history |
| Stray SQL `UPDATE`/`DELETE` | SQLite triggers abort |
| Raw file edit (triggers dropped) | Hash chain breaks at the edited entry |
| Edit the rule table to flip a status | Projection check: replays audit events and compares |
| Edit a stored decision | Decision SHA-256 is recorded in the chain |
| Truncate the tail | Only caught with an externally anchored head (`--expected-head`) |
| Rewrite the entire chain consistently | **Not caught** without an external anchor or signing; see "Next" |

## What is stubbed or simulated

- **Data:** entirely synthetic. FX rates, fees, escalation costs and ground-truth
  probabilities are invented.
- **Rules:** structured predicates only. No natural-language memory, no LLM in the
  loop, and no integration with a real agent framework.
- **Canary:** simulated "live" traffic from a different seed. Outcomes resolve
  instantly. In reality they lag by weeks.
- **Human analyst:** escalations are resolved optimally at a fixed cost.
- **Authn/authz:** the `X-Actor` header is trusted. The approver list lives in config.
- **Head anchoring:** manual (`--expected-head`). Nothing publishes the head anywhere.
- **Assurance:** runs on demand (`agent-gate assurance`), not on a schedule.
- **Storage:** SQLite only. A Postgres implementation of `Storage` is not written.
- **Multi-tenant, UI, notifications:** none.

## Next (recommended order)

1. Wrap a real memory write path: a design partner's agent writes memory, and
   agent-gate intercepts it as a candidate. Support natural-language rules by
   replaying the actual agent (LLM) on held-out cases.
2. Submission budgets per agent/rule family, and rotating held-out sets (gap G-1).
3. Delayed-outcome canary (gap G-2). The sequential test exists (`stats =
   "evalue"`); outcomes still resolve instantly.
4. Signed checkpoints of the audit head (HMAC/KMS or an external transparency log).
5. Real authn (OIDC/mTLS) and role-based approvals.
6. Postgres storage; a second domain pack (reconciliation or payments exceptions).
