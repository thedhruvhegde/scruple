"""ACH returns environment (roadmap 041-042): rules, seeded cases, event-sourced engine.

All data is synthetic. Rules and their sources: `scruple.sim.ach.rules` and
docs/WORKFLOWS.md.
"""

from scruple.sim.ach.engine import (
    AcceptReturn,
    AchAction,
    AchEngine,
    AchOutcome,
    ActionError,
    ActionResult,
    Dishonor,
    Reinitiate,
    WriteOff,
)
from scruple.sim.ach.generator import generate_ach_case, generate_ach_cases
from scruple.sim.ach.models import FAMILIES, AchCase, AchFamily, Allocation

__all__ = [
    "FAMILIES",
    "AcceptReturn",
    "AchAction",
    "AchCase",
    "AchEngine",
    "AchFamily",
    "AchOutcome",
    "ActionError",
    "ActionResult",
    "Allocation",
    "Dishonor",
    "Reinitiate",
    "WriteOff",
    "generate_ach_case",
    "generate_ach_cases",
]
