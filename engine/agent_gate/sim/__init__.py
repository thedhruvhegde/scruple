"""Deterministic, synthetic dispute-workflow simulator."""

from agent_gate.sim.engine import RunResult, Simulator, oracle, outcome_for
from agent_gate.sim.generator import DEFAULT_PROFILE, Profile, generate_cases
from agent_gate.sim.models import CaseResult, DisputeCase, Scenario
from agent_gate.sim.policy import Policy, PolicyRule

__all__ = [
    "DEFAULT_PROFILE",
    "CaseResult",
    "DisputeCase",
    "Policy",
    "PolicyRule",
    "Profile",
    "RunResult",
    "Scenario",
    "Simulator",
    "generate_cases",
    "oracle",
    "outcome_for",
]
