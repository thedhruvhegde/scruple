"""Process verifiers (064): what the agent read before it asserted anything.

Before the agent asserts anything to the issuer (`represent`), two checks read the
trace (docs/DECISIONS.md D-020 split them):

- `PROC-01` (hard): a `get_evidence` for every document it cites, before the
  representment that cites it. Citing a document the agent never opened is a
  statement about evidence nobody looked at (X6, the summary response).
- `PROC-02` (soft): the recommended investigation steps, in this order, before the
  representment (ASSUMPTION A-PROC, ours, modelled on the "reasonable investigation"
  idea in docs/research/VERIFIERS.md 3.1 and V-PROC-02; not a network rule; not the
  same id space as that design note):
  1. `get_case`: read the case, its stage and deadline;
  2. `list_evidence`: looked at what the merchant holds;
  3. per reason-code category: consumer disputes `request_info` (the cardholder's
     statement); processing errors and authorization `get_ledger_view` (prior refunds,
     duplicates and amounts are on the ledger).
  Real models that investigate fully but in another order, or that rely on the case
  shown when the episode starts, fail it; that is a preference, so it lowers quality
  and never gates.

Only representments are held to either. Conceding without looking is a cost
question, which the counterfactual verifier prices. Each explanation lists every
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
NO_REPRESENTMENT = "no representment: nothing to investigate first"


def _required(category: str) -> tuple[str, ...]:
    return ("get_case", "list_evidence", *CATEGORY_STEPS.get(category, ()))


def _reps(r: EpisodeRecord) -> tuple[tuple[TraceEvent, ...], list[TraceEvent]]:
    calls = r.tool_calls(ok=True)
    return calls, [e for e in calls if e.tool == "represent"]


def _citations(r: EpisodeRecord) -> Verdict:
    """PROC-01: every cited document was opened before the representment."""
    calls, reps = _reps(r)
    if not reps:
        return True, 1.0, (), NO_REPRESENTMENT
    met = total = 0
    ev: list[EvidenceRef] = []
    for rep in reps:
        read = {
            (e.args or {}).get("evidence_id")
            for e in calls
            if e.seq < rep.seq and e.tool == "get_evidence"
        }
        for doc in (rep.args or {}).get("evidence_ids", []):
            total += 1
            if doc in read:
                met += 1
            else:
                ev.append(EvidenceRef(source="document", ref=str(doc), note="cited unread"))
    if ev:
        return False, met / total, ev, "cited an unread document"
    return True, 1.0, (), "every cited document was opened first"


def _steps(r: EpisodeRecord) -> Verdict:
    """PROC-02: the recommended steps, in order, before each representment."""
    calls, reps = _reps(r)
    if not reps:
        return True, 1.0, (), NO_REPRESENTMENT
    steps = _required(r.case.category.value)
    met = total = 0
    missing: list[str] = []
    ev: list[EvidenceRef] = []
    for rep in reps:
        before = [e for e in calls if e.seq < rep.seq]
        # each step must appear after the previous one's first call
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
    ok = not missing
    why = "investigated before responding" if ok else "; ".join(dict.fromkeys(missing))
    return ok, met / total if total else 1.0, ev, why


PROCESS = FnCheck(
    "PROC-01",
    "every cited document opened before the representment",
    _citations,
    weight=1.0,
    hard=True,
    guards=("X6", "X9", "X11"),
)
PROCESS_STEPS = FnCheck(
    "PROC-02",
    "recommended investigation steps, in order, before a representment",
    _steps,
    weight=1.0,
    hard=False,
    guards=("X6",),
)

__all__ = ["CATEGORY_STEPS", "PROCESS", "PROCESS_STEPS"]
