"""Hand-written agents: a seeded random floor and an expert rule set.

The existing reference policies (`evidence`, `always_accept`) are scored as they are,
through the benchmark's built-in names; they are not rewritten here.

`ExpertAgent` is written from docs/RULES.md and the reason codes' evidence
requirements, never from outcomes:

1. A refund in flight with no settled refund record on file: wait while the
   deadline allows (a settled refund makes the dispute moot, A-23; conceding while
   one is in flight refunds twice, which INV-01 fails).
2. A genuine settled refund record, or Visa CE 3.0 history on an eligible 10.4 case
   (S2-S4, A-28): represent with it.
3. Inquiry or chargeback: represent when every required kind of the reason code is
   held as a genuine document (the S1 key-evidence mapping, A-15); otherwise concede.
   Partial coverage is conceded on purpose: under the issuer odds (A-21) it still wins
   often, but the generator holds partial evidence mostly for valid claims (A-19), so
   those wins are mostly unfair (OUT-01).
4. Pre-arbitration: escalate only with full coverage and a disputed amount of at
   least `escalate_min` USD minor units. With full coverage A-21 gives arbitration
   odds of about 0.05 + 0.9 * 0.95 = 0.905; losing adds the arbitration fee (A-17,
   $575 or $600), so the expected saving is positive above roughly $70. The
   threshold is tuned on dev.
5. Never cite a document that fails the genuineness checks, never upload a document.

Every number above is a simulator ASSUMPTION or a cited rule in docs/RULES.md; none
is a real-world dispute statistic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from scruple.zoo.context import Act, DecisionContext

NEVER = 10**12


@dataclass(frozen=True)
class RandomAgent:
    """Uniform over the legal acts, seeded per (seed, episode, step). The floor."""

    seed: int = 0
    name: str = "random"
    kind: str = "random"

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        rng = random.Random(f"zoo-random:{self.seed}:{ctx.episode_id}:{ctx.step}")
        return rng.choice(legal)


@dataclass(frozen=True)
class ExpertAgent:
    escalate_min: int = 7_500
    name: str = "expert_rules"
    kind: str = "rule"

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        if Act.WAIT in legal and ctx.refund_pending():
            return Act.WAIT
        strong = bool(ctx.moot_docs() or ctx.ce3_docs())
        full = ctx.coverage() >= 1.0
        if Act.REPRESENT in legal and (strong or full):
            return Act.REPRESENT
        if (
            Act.ESCALATE in legal
            and (full or strong)
            and int(ctx.case["amount_minor"]) >= self.escalate_min
        ):
            return Act.ESCALATE
        if Act.CONCEDE in legal:
            return Act.CONCEDE
        for a in (Act.REPRESENT, Act.ESCALATE, Act.WAIT):  # concede not open: answer
            if a in legal:
                return a
        return legal[0]  # unreachable: the driver never asks without an honest act


__all__ = ["NEVER", "ExpertAgent", "RandomAgent"]
