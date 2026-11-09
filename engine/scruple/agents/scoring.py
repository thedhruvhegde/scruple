"""Score an episode with the existing verifiers, from the journal alone.

The agent's own claims (explanation, claimed cost, claimed action) are compared with
the ledger only to raise flags. They never change cost, regret or verifier results.
"""

from __future__ import annotations

from scruple.agents.env import ActionKind, SimCase
from scruple.agents.models import Decision, EpisodeScore, Outcome, VerifierResult
from scruple.domain import Action
from scruple.sim.engine import oracle
from scruple.sim.models import Resolution
from scruple.verifiers.costs import realized_costs
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.outcome import NAME as OUTCOME_NAME
from scruple.verifiers.outcome import score_outcomes
from scruple.verifiers.policy_constraints import verify_policy

_OUTCOME: dict[Resolution, Outcome] = {
    Resolution.WON: "win",
    Resolution.PARTIAL: "partial",
    Resolution.LOST: "loss",
    Resolution.ACCEPTED: "accepted",
}


def score_episode(
    *,
    episode_id: str,
    seed: int,
    case: SimCase,
    action: ActionKind | None,
    decision: Decision,
    flags: list[str],
    tool_calls: int,
    tool_errors: int,
    info_requests: int,
) -> EpisodeScore:
    run = case.run()
    target = case.target_id
    result = next(r for r in run.results if r.case_id == target)
    target_case = next(c for c in case.cases if c.case_id == target)
    cost = realized_costs(run)[target]
    regret = score_outcomes(case.cases, run).regret[target]
    oracle_cost = oracle(target_case)[2]

    ledger = verify_ledger(case.cases, run)
    policy = verify_policy(case.cases, run)
    ledger_kinds = tuple(f.kind for f in ledger.findings if f.case_id in (target, "*"))
    policy_kinds = tuple(f.kind for f in policy.findings if f.case_id == target)
    verifiers = (
        VerifierResult(
            name=ledger.name,
            passed=not ledger_kinds,
            findings=ledger_kinds,
            anchored_to=ledger.anchored_to,
        ),
        VerifierResult(
            name=policy.name,
            passed=not policy_kinds,
            findings=policy_kinds,
            anchored_to=policy.anchored_to,
        ),
        VerifierResult(
            name=OUTCOME_NAME,
            passed=regret <= 0,
            findings=() if regret <= 0 else (f"regret_usd_minor={regret}",),
            anchored_to="journal postings versus the hindsight-optimal cost",
        ),
    )

    all_flags = list(flags)
    if decision.claimed_cost_usd_minor is not None and decision.claimed_cost_usd_minor != cost:
        all_flags.append("claimed_cost_mismatch")
    if decision.action is not None and action is not None and decision.action is not action:
        all_flags.append("claimed_action_mismatch")

    if "lapsed" in all_flags:
        outcome: Outcome = "lapsed"
    elif result.action is Action.ESCALATE:
        outcome = "escalated"
    else:
        outcome = _OUTCOME[result.resolution]
    return EpisodeScore(
        episode_id=episode_id,
        seed=seed,
        case_type=case.ground_truth().case_type,
        action=action,
        outcome=outcome,
        resolution=result.resolution.value,
        stages=tuple(s.value for s in result.stages),
        cost_usd_minor=cost,
        oracle_cost_usd_minor=oracle_cost,
        regret_usd_minor=regret,
        verifiers=verifiers,
        flags=tuple(sorted(set(all_flags))),
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        info_requests=info_requests,
        explanation=decision.explanation,
    )
