"""Realized cost per case, recomputed from the journal (what money actually moved)."""

from __future__ import annotations

from collections import defaultdict

from agent_gate.domain import Action
from agent_gate.sim.engine import RunResult
from agent_gate.sim.models import ESCALATION_COST_USD, to_usd


def realized_costs(run: RunResult) -> dict[str, int]:
    paid: dict[str, int] = defaultdict(int)
    for e in run.journal:
        if e.case_id is None:
            continue
        for p in e.postings:
            if p.account.startswith("merchant:") and p.amount_minor < 0:
                paid[e.case_id] += to_usd(-p.amount_minor, e.currency)
    return {
        r.case_id: paid[r.case_id] + (ESCALATION_COST_USD if r.action is Action.ESCALATE else 0)
        for r in run.results
    }
