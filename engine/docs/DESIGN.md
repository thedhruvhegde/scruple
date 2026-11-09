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
| `agent_gate/gate` | `engine` (pure shadow and canary evaluation returning a `Verdict`), `stats` (seeded paired bootstrap), `regression` (golden suite), `redteam` (runs attacks), `config`. |
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
3. Delayed-outcome canary with sequential testing (gap G-2).
4. Signed checkpoints of the audit head (HMAC/KMS or an external transparency log).
5. Real authn (OIDC/mTLS) and role-based approvals.
6. Postgres storage; a second domain pack (reconciliation or payments exceptions).
