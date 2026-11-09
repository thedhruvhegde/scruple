"""Reference agents, so every scorecard has something to compare against.

- `rule_based`: the synthetic baseline policy (`scruple.sim.library.BASELINE_RULES`)
  applied to the observation. Sees only whitelisted fields, like any agent.
- `always_accept`: accepts every chargeback. The floor: no automation risk, no wins.
- `oracle`: takes the cheapest action that breaks no policy constraint, using hidden
  ground truth. It is a ceiling, not an agent: it only works when the episode is run
  with `ground_truth=True`, and scorecards say so.
"""

from __future__ import annotations

from scruple.agents.env import AGENT_ACTION, ActionKind
from scruple.agents.interface import Toolbox
from scruple.agents.models import Decision, Observation
from scruple.sim.library import baseline_policy

_POLICY = baseline_policy()


def _act(tools: Toolbox, kind: ActionKind, reason: str) -> None:
    if kind is ActionKind.REPRESENT:
        ids = [e["evidence_id"] for e in tools.call("list_evidence")["evidence"]]
        tools.call("represent", evidence_ids=ids, narrative=reason)
    else:
        tools.call(kind.value, reason=reason)


def rule_based(obs: Observation, tools: Toolbox) -> Decision:
    action, ref = _POLICY.decide(obs.fields)
    kind = AGENT_ACTION[action]
    reason = f"baseline rule {ref}" if ref != "default" else "no baseline rule matched"
    _act(tools, kind, reason)
    return Decision(explanation=f"{kind.value}: {reason}.")


def always_accept(obs: Observation, tools: Toolbox) -> Decision:
    tools.call("accept_liability", reason="always accept")
    return Decision(explanation="Accepted: this agent accepts every dispute.")


def oracle(obs: Observation, tools: Toolbox) -> Decision:
    truth = tools.ground_truth
    if truth is None:
        raise ValueError("the oracle needs ground truth: run it with ground_truth=True")
    _act(tools, truth.best_action, "hindsight-optimal compliant action")
    return Decision(explanation=f"{truth.best_action.value}: oracle with ground truth.")


BASELINES = {"rule_based": rule_based, "always_accept": always_accept, "oracle": oracle}
