"""Promotion gate: shadow replay, paired comparison, regression guards and canary."""

from scruple.gate.config import GateConfig
from scruple.gate.engine import Verdict, evaluate_canary, evaluate_shadow, policy_from
from scruple.gate.models import Decision, DecisionRecord, Outcome, Stage

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
