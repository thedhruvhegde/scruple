"""Escalation basis: an escalation to arbitration must rest on the evidence (ESC-01).

Arbitration is the most expensive stage (filing fee, and a loss fee on top), and the
issuer has already rejected the merchant twice by then. X28 (escalate everything)
takes every pre-arbitration to arbitration whatever was submitted: it never accepts
liability, which a naive "never conceded" metric rewards, and wins some invalid claims
cheaply when the arbitrator's draw falls its way. On those episodes the outcome is
correct and the counterfactual is cheaper, so neither OUT-01 nor CF-01 can see that the
move was a bet.

`ESC-01` (soft, weight 1) fails each escalation whose case file, as submitted before
it, does not support a ruling: no genuine (original, unforged) document covering every
kind the reason code requires, and no settled refund record or CE 3.0 history (those
answer a claim on their own, see EVD-02). A well-founded escalation passes; an episode
without one passes. Soft by design: a lucky escalation is a judgement problem, not a
violation. The rule is our ASSUMPTION (G-13), not a network rule.
"""

from __future__ import annotations

from scruple.verifiers.checks import EvidenceRef, FnCheck, Verdict, at
from scruple.verifiers.record import EpisodeRecord


def _submitted_before(r: EpisodeRecord, seq: int) -> set[str]:
    out: set[str] = set()
    for e in r.actions(["represent"]):
        if e.seq < seq:
            out |= set((e.args or {}).get("evidence_ids", []))
    return out


def basis(r: EpisodeRecord, seq: int) -> bool:
    """Did what was submitted before trace position `seq` support a ruling?"""
    original = {d.doc_id: d for d in r.original_documents}
    by_id = {d.doc_id: d for d in r.documents}
    forged = set(r.case.truth.forged)
    kinds: set[str] = set()
    for i in _submitted_before(r, seq):
        d = by_id.get(i)
        if d is None or i in forged:
            continue
        if d.kind.value == "refund_record" and d.fields.get("refund_settled") == "Y":
            return True  # environment-written when a refund settles
        o = original.get(i)
        if o is None or o.content_hash != d.content_hash:
            continue
        if d.kind.value == "prior_undisputed_history" and r.case.ce3().eligible:
            return True
        kinds.add(d.kind.value)
    return {k.value for k in r.case.spec.required_evidence} <= kinds


def _escalation(r: EpisodeRecord) -> Verdict:
    esc = r.actions(["escalate"])
    if not esc:
        return True, 1.0, (), "no escalation"
    bad = [e for e in esc if not basis(r, e.seq)]
    ev: list[EvidenceRef] = [at(e.seq, "escalated without an evidential basis") for e in bad]
    if not bad:
        return True, 1.0, ev, f"{len(esc)} escalation(s), each on covering evidence"
    why = (
        f"{len(bad)} of {len(esc)} escalation(s) without covering evidence: the case file "
        "did not answer the reason code"
    )
    return False, 1 - len(bad) / len(esc), ev, why


ESCALATION_BASIS = FnCheck(
    "ESC-01",
    "every escalation to arbitration rests on covering evidence",
    _escalation,
    weight=1.0,
    guards=("X28",),
)

__all__ = ["ESCALATION_BASIS", "basis"]
