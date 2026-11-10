"""The wire models the client sends and receives: the server's own classes.

Nothing here is redefined. Each name is imported from the module the HTTP API uses,
so the client and server cannot drift apart.
"""

from __future__ import annotations

from scruple.api.app import MAX_PAGE, TOTAL_COUNT, DecisionOut, RegisterIn, RollbackIn
from scruple.domain import RuleSpec
from scruple.gate.models import Decision, Outcome, Stage
from scruple.gate.shadow import BatchResult, LiveDecision, LiveDecisionBatch, ShadowSummary
from scruple.registry import Provenance, RuleStatus, RuleVersion
from scruple.service import IntegrityReport
from scruple.version import BuildInfo

__all__ = [
    "MAX_PAGE",
    "TOTAL_COUNT",
    "BatchResult",
    "BuildInfo",
    "Decision",
    "DecisionOut",
    "IntegrityReport",
    "LiveDecision",
    "LiveDecisionBatch",
    "Outcome",
    "Provenance",
    "RegisterIn",
    "RollbackIn",
    "RuleSpec",
    "RuleStatus",
    "RuleVersion",
    "ShadowSummary",
    "Stage",
]
