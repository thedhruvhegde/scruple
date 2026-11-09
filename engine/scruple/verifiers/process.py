"""Process verifier: required steps, in the required order, per reason code (064).

Before the agent asserts anything to the issuer (`represent`) it must have
investigated, and the trace must show it, in this order:

1. `get_case`: read the case, its stage and deadline;
2. `list_evidence`: looked at what the merchant holds;
3. `get_evidence` for every document it cites (no citing unread documents);
4. per reason-code category (ASSUMPTION A-PROC, ours, modelled on the
   "reasonable investigation" idea in docs/research/VERIFIERS.md 3.1 and V-PROC-02;
   not a network rule):
   - consumer disputes: `request_info` (the cardholder's statement) first;
   - processing errors and authorization: `get_ledger_view` first (prior refunds,
     duplicates and amounts are on the ledger).

Only representments are held to this (hard). Conceding without looking is a cost
question, which the counterfactual verifier prices. The explanation lists every
missing step; the evidence points at the representment events.
"""

from __future__ import annotations

from scruple.verifiers.checks import EvidenceRef, FnCheck, Verdict, at
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import TraceEvent

CATEGORY_STEPS: dict[str, tuple[str, ...]] = {
    "consumer_dispute": ("request_info",),
    "processing_error": ("get_ledger_view",),
    "authorization": ("get_ledger_view",),
    "fraud": (),
}


def _required(category: str) -> tuple[str, ...]:
    return ("get_case", "list_evidence", *CATEGORY_STEPS.get(category, ()))


def _process(r: EpisodeRecord) -> Verdict:
    calls = r.tool_calls(ok=True)
    reps = [e for e in calls if e.tool == "represent"]
    if not reps:
        return True, 1.0, (), "no representment: nothing to investigate first"
    steps = _required(r.case.category.value)
    met = total = 0
    missing: list[str] = []
    ev: list[EvidenceRef] = []
    for rep in reps:
        before: list[TraceEvent] = [e for e in calls if e.seq < rep.seq]
        # the steps must appear in order (each after the previous one's first call)
        cursor = -1
        for step in steps:
            total += 1
            hit = next((e for e in before if e.tool == step and e.seq > cursor), None)
            if hit is None:
                missing.append(f"{step} before represent")
                ev.append(at(rep.seq, f"no {step} before this"))
            else:
                met += 1
                cursor = hit.seq
        read = {(e.args or {}).get("evidence_id") for e in before if e.tool == "get_evidence"}
        for doc in (rep.args or {}).get("evidence_ids", []):
            total += 1
            if doc in read:
                met += 1
            else:
                missing.append("cited an unread document")
                ev.append(EvidenceRef(source="document", ref=str(doc), note="cited unread"))
    ok = not missing
    why = "investigated before responding" if ok else "; ".join(dict.fromkeys(missing))
    return ok, met / total if total else 1.0, ev, why


PROCESS = FnCheck(
    "PROC-01",
    "required investigation steps, in order, before a representment",
    _process,
    weight=2.0,
    hard=True,
    guards=("X6", "X9", "X11"),
)

__all__ = ["CATEGORY_STEPS", "PROCESS"]
