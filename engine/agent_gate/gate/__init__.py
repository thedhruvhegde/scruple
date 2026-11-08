"""Promotion gate: shadow replay, paired comparison, regression guards and canary."""

from agent_gate.gate.config import GateConfig
from agent_gate.gate.engine import Verdict, evaluate_canary, evaluate_shadow, policy_from
from agent_gate.gate.models import Decision, DecisionRecord, Outcome, Stage

__all__ = [
    "Decision",
    "DecisionRecord",
    "GateConfig",
    "Outcome",
    "Stage",
    "Verdict",
    "evaluate_canary",
    "evaluate_shadow",
    "policy_from",
]
