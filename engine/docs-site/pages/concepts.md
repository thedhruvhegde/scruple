# Concepts

## Rules and versions

A learned rule is data, not code: structured predicates over a whitelist of
observable fields, so ground truth, scenario labels and identifiers cannot be
addressed. Each rule version is content-hashed and immutable; only its status moves,
along a fixed state machine (candidate, shadow, canary, active, retired, rejected).

## Verifiers

Verifiers score what happened, from the double-entry ledger and the hash-chained
episode trace, never from what the agent reports: ledger invariants, outcome regret
against the hindsight-best action, per-case policy constraints, and checks on
evidence and self-reports. Each verifier ships with an attack that fools a naive
strawman and is stopped by the gate.

## The gate

1. **Shadow.** The candidate is replayed against held-out synthetic cases (an
   operator-held seed) next to the baseline, paired, case by case. Hard checks, a
   golden suite, per case-type slices and the statistics decide: promote, hold or
   block. Too little data is a verdict ("insufficient evidence"), never a silent pass.
2. **Canary.** A deterministic slice of traffic runs the candidate; realised cost and
   hard checks decide: canary passed, hold, or roll back.
3. **Approval.** A named approver activates it. The owner, the proposer and the
   learning agent can never approve their own rule. An approval policy can require N
   of M named approvers and a role.
4. **Assurance.** The live policy is re-checked; a failure rolls back the latest
   activation.

Every step is a `Decision` with a markdown evidence report and an evidence pack
(JSON and PDF).

## The audit chain

Every state change is an entry in a per-tenant hash chain. `scruple audit verify`
recomputes the chain and checks that the rule table and every stored decision match
what the chain says happened. History tables are append-only in the database
(triggers), not only in code.

## Tenants, identities and roles

Every credential names a tenant; every route sees only the caller's tenant. API keys
(scrypt-hashed, shown once), OIDC bearer tokens and SAML sign-on (console) carry
roles:

| role | permissions |
|---|---|
| proposer | read, propose |
| service | read, shadow |
| approver | read, approve |
| risk_admin | read, approve, admin |
| auditor | read |

No single credential may hold both propose and approve. Every route declares the
permission it needs; a route that declares none is closed.

Tenants exist once anything is stored for them. The operator's command line lists
and records them (`scruple tenant list|show|create`), caps a tenant's shadow replays
per UTC day (`scruple tenant quota T --gate-runs-per-day N`; a refused evaluation is a
429) and offboards one (`scruple tenant offboard T`, a dry run until `--yes`): its keys
are revoked, its imported-shadow rows and case store are purged through the retention
path, each step goes into its audit chain, and the id is never served again.

## Usage and billing

Usage is metered from the gate's own decisions: gate runs, cases replayed, rollouts,
per tenant, append-only and idempotent (`scruple usage report`, `GET /usage`).
Billing hooks push per-day totals to Stripe metered billing as meter events, in test
mode by default (`scruple billing plan|push`).

## Real and synthetic

The simulator is synthetic. Real-data results name their dataset (public-domain CFPB
complaints today). Nothing is presented as a partner's outcome until a partner's data
has run.
