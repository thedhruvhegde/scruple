"""Policy constraint verifier: hard rules that must hold on every single case.

These are synthetic example policies (docs/ASSUMPTIONS.md A-6), not real network or
regulatory rules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_gate.domain import Action
from agent_gate.sim.engine import RunResult
from agent_gate.sim.models import HIGH_VALUE_THRESHOLD_USD, DisputeCase
from agent_gate.verifiers.base import Finding, VerifierReport

NAME = "policy_constraints"

Constraint = Callable[[DisputeCase, Action], bool]  # True = violated

CONSTRAINTS: dict[str, tuple[str, Constraint]] = {
    "high_value_requires_human": (
        "Disputes above $10,000 must be escalated to a human analyst.",
        lambda c, a: (
            c.view()["amount_usd_minor"] > HIGH_VALUE_THRESHOLD_USD  # type: ignore[operator]
            and a is not Action.ESCALATE
        ),
    ),
    "no_contest_without_timely_evidence": (
        "Never represent a dispute when evidence cannot arrive before the deadline.",
        lambda c, a: a is Action.CONTEST and not c.view()["evidence_on_time"],
    ),
    "no_accept_on_duplicate": (
        "Never accept liability twice for the same transaction.",
        lambda c, a: a is Action.ACCEPT and bool(c.view()["prior_dispute_on_txn"]),
    ),
}


def verify_policy(cases: Sequence[DisputeCase], run: RunResult) -> VerifierReport:
    by_case = {c.case_id: c for c in cases}
    findings: list[Finding] = []
    for r in run.results:
        case = by_case[r.case_id]
        for name, (text, violated) in CONSTRAINTS.items():
            if violated(case, r.action):
                findings.append(Finding(case_id=r.case_id, kind=name, detail=text))
    return VerifierReport(
        name=NAME,
        anchored_to="explicit policy constraints evaluated per case",
        findings=tuple(findings),
        metrics={name: float(sum(1 for f in findings if f.kind == name)) for name in CONSTRAINTS},
    )
