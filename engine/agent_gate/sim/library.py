"""Synthetic rule library: the demo baseline policy and example learned rules."""

from __future__ import annotations

from agent_gate.domain import Action, Condition, RuleSpec
from agent_gate.sim.models import HIGH_VALUE_THRESHOLD_USD
from agent_gate.sim.policy import Policy, PolicyRule


def C(field: str, op: str, value: bool | int | str) -> Condition:  # noqa: N802
    return Condition.model_validate({"field": field, "op": op, "value": value})


BASELINE_RULES: dict[str, RuleSpec] = {
    "base-high-value-escalate": RuleSpec(
        description="Disputes above $10,000 always go to a human analyst.",
        conditions=(C("amount_usd_minor", "gt", HIGH_VALUE_THRESHOLD_USD),),
        action=Action.ESCALATE,
        priority=0,
    ),
    "base-late-evidence-accept": RuleSpec(
        description="If evidence cannot arrive before the deadline, accept.",
        conditions=(C("evidence_on_time", "eq", False),),
        action=Action.ACCEPT,
        priority=10,
    ),
    "base-duplicate-contest": RuleSpec(
        description="Contest disputes on a transaction that was already disputed.",
        conditions=(C("prior_dispute_on_txn", "eq", True),),
        action=Action.CONTEST,
        priority=20,
    ),
    "base-fraud-3ds-contest": RuleSpec(
        description="Contest fraud claims on 3-D Secure authenticated transactions.",
        conditions=(C("reason_code", "eq", "fraud"), C("three_ds", "eq", True)),
        action=Action.CONTEST,
        priority=30,
    ),
    "base-refund-contest": RuleSpec(
        description="Contest when a partial refund was already issued.",
        conditions=(C("has_prior_refund", "eq", True),),
        action=Action.CONTEST,
        priority=40,
    ),
    "base-fx-contest": RuleSpec(
        description="Contest when dispute and settlement currencies differ.",
        conditions=(C("currency_mismatch", "eq", True),),
        action=Action.CONTEST,
        priority=45,
    ),
}

# A good learned rule: genuinely improves outcomes, and respects existing precedence.
GOOD_RULE_ID = "inr-delivered-contest"
GOOD_RULE = RuleSpec(
    description=("Contest 'item not received' disputes when the carrier confirmed delivery."),
    conditions=(C("reason_code", "eq", "not_received"), C("delivery_confirmed", "eq", True)),
    action=Action.CONTEST,
    priority=50,
)

# A subtly bad learned rule. Its content looks almost identical to the good one and
# it improves the average, but the agent marked it high priority ("this always
# works"), so it pre-empts the late-evidence and high-value safeguards. It breaks
# the late-evidence edge case and violates the escalation policy on large disputes.
BAD_RULE_ID = "inr-delivered-contest-fast"
BAD_RULE = RuleSpec(
    description=(
        "Always contest 'item not received' disputes with confirmed delivery; "
        "learned as a high-confidence override."
    ),
    conditions=(C("reason_code", "eq", "not_received"), C("delivery_confirmed", "eq", True)),
    action=Action.CONTEST,
    priority=5,
)


def baseline_policy() -> Policy:
    return Policy(
        rules=tuple(
            PolicyRule(rule_id=rid, ref=f"{rid}@v1", spec=spec)
            for rid, spec in BASELINE_RULES.items()
        )
    )
