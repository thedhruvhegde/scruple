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
