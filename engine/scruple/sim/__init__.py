"""Deterministic, synthetic dispute-workflow simulator."""

from scruple.sim.engine import RunResult, Simulator, oracle, outcome_for
from scruple.sim.generator import DEFAULT_PROFILE, Profile, generate_cases
from scruple.sim.models import CaseResult, DisputeCase, Scenario
from scruple.sim.policy import Policy, PolicyRule

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
