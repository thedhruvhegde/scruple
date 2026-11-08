"""Dispute outcome verifier.

Anchored to ground-truth labels and the ledger: cost per case is recomputed from
journal postings (what actually moved), and compared against the hindsight-optimal
cost derived from ground truth. It never reads the regret the run reports about
itself.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from agent_gate.domain import Action
from agent_gate.sim.engine import RunResult, oracle
from agent_gate.sim.models import DisputeCase, Scenario
from agent_gate.verifiers.costs import realized_costs

NAME = "dispute_outcome"


class OutcomeScores(BaseModel):
    model_config = ConfigDict(frozen=True)

    regret: dict[str, int]  # case_id -> regret, USD minor units
    action: dict[str, Action]
    scenario: dict[str, Scenario]
    mean_regret: float
    accuracy: float  # share of cases resolved at hindsight-optimal cost
    escalation_rate: float
    slice_mean_regret: dict[Scenario, float]


def score_outcomes(cases: Sequence[DisputeCase], run: RunResult) -> OutcomeScores:
    by_case = {c.case_id: c for c in cases}
    paid = realized_costs(run)
    regret: dict[str, int] = {}
    actions: dict[str, Action] = {}
    scen: dict[str, Scenario] = {}
    for r in run.results:
        case = by_case[r.case_id]
        regret[r.case_id] = paid[r.case_id] - oracle(case)[2]
        actions[r.case_id] = r.action
        scen[r.case_id] = case.scenario
    n = max(len(regret), 1)
    slices: dict[Scenario, list[int]] = defaultdict(list)
    for cid, v in regret.items():
        slices[scen[cid]].append(v)
    return OutcomeScores(
        regret=regret,
        action=actions,
        scenario=scen,
        mean_regret=sum(regret.values()) / n,
        accuracy=sum(1 for v in regret.values() if v <= 0) / n,
        escalation_rate=sum(1 for a in actions.values() if a is Action.ESCALATE) / n,
        slice_mean_regret={s: sum(v) / len(v) for s, v in sorted(slices.items())},
    )
