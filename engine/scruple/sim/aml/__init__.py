"""AML alert triage environment: synthetic transaction graphs, one alert per case.

All data is synthetic. Sources and assumptions: docs/WORKFLOWS.md (`aml`).
"""

from scruple.sim.aml.engine import (
    AmlAction,
    AmlEngine,
    AmlOutcome,
    CloseAlert,
    Escalate,
    FileSar,
    RequestInfo,
    decision_cost,
)
from scruple.sim.aml.generator import generate_aml_case, generate_aml_cases
from scruple.sim.aml.models import FAMILIES, TYPOLOGIES, AmlCase, AmlFamily

__all__ = [
    "FAMILIES",
    "TYPOLOGIES",
    "AmlAction",
    "AmlCase",
    "AmlEngine",
    "AmlFamily",
    "AmlOutcome",
    "CloseAlert",
    "Escalate",
    "FileSar",
    "RequestInfo",
    "decision_cost",
    "generate_aml_case",
    "generate_aml_cases",
]
