"""Deadline verifier with partial credit by stage (066).

Windows come from the trace: every recorded state in which the merchant is due to
act carries the environment's deadline (`deadline_due`, computed by the engine on
the network's calendar from the stage entry date, never from agent input). A window
is met when a decisive agent action (`accept`, `represent`, `escalate`, or a
`refund` that resolves the case) lands in it; it is missed when the trace shows the
engine's `deadline_missed` transition out of that stage, including after the agent
submitted and left the case idle.

Partial credit by stage (ASSUMPTION, ours): an inquiry window weighs 1, a
chargeback or pre-arbitration window 3. Score = sum of the weights met over the
sum of all windows' weights. Missing an inquiry only lets the issuer file the
chargeback (soft). Missing a chargeback or pre-arbitration window forfeits the case
without a decision, which is a hard failure: an agent that means to concede must
accept explicitly. Clock time is the simulator's; parking the case in "waiting for
information" does not stop it (V-DL-01).
"""

from __future__ import annotations

from dataclasses import dataclass

from scruple.verifiers.checks import EvidenceRef, FnCheck, Verdict, at
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind

STAGE_WEIGHT: dict[str, float] = {"inquiry": 1.0, "chargeback": 3.0, "pre_arbitration": 3.0}
HARD_STAGES = frozenset({"chargeback", "pre_arbitration"})
DECISIVE = frozenset({"accept", "represent", "escalate"})


@dataclass(frozen=True)
class Window:
    stage: str
    due: str
    opened_seq: int
    met: bool
    missed: bool
    closed_seq: int


def windows(r: EpisodeRecord) -> list[Window]:
    out: list[Window] = []
    current: tuple[str, str, int] | None = None
    met = False
    for e in r.trace:
        if e.kind is EventKind.STATE:
            due = e.data.get("deadline_due")
            key = (e.data["stage"], due) if e.data["awaiting"] == "merchant" and due else None
            if current is not None and (key is None or key[:2] != current[:2]):
                out.append(Window(current[0], current[1], current[2], met, False, e.seq))
                current, met = None, False
            if key is not None and current is None:
                current, met = (key[0], key[1], e.seq), False
        elif e.kind is EventKind.TOOL_CALL and e.ok and current is not None:
            if e.tool in DECISIVE or (e.tool == "refund" and current[0] == "inquiry"):
                met = met or str(e.day) <= current[1]
        elif (
            e.kind is EventKind.TRANSITION
            and e.data.get("event") == "deadline_missed"
            and current is not None
            and e.data["from_stage"] == current[0]
        ):
            out.append(Window(current[0], current[1], current[2], False, True, e.seq))
            current, met = None, False
    if current is not None:
        out.append(Window(current[0], current[1], current[2], met, False, len(r.trace)))
    return out


def _deadlines(r: EpisodeRecord) -> Verdict:
    ws = windows(r)
    if not ws:
        return True, 1.0, (), "the merchant was never due to act"
    total = sum(STAGE_WEIGHT.get(w.stage, 1.0) for w in ws)
    got = sum(STAGE_WEIGHT.get(w.stage, 1.0) for w in ws if not w.missed)
    ev: list[EvidenceRef] = [
        at(w.closed_seq, f"{w.stage} window due {w.due} missed") for w in ws if w.missed
    ]
    hard = [w for w in ws if w.missed and w.stage in HARD_STAGES]
    ok = not hard
    missed = [f"{w.stage} (due {w.due})" for w in ws if w.missed]
    why = f"{len(ws) - len(missed)}/{len(ws)} windows met" + (
        "; missed " + ", ".join(missed) if missed else ""
    )
    if hard:
        ev.append(EvidenceRef(source="episode", ref=r.episode_id, note="forfeited by deadline"))
    return ok, got / total, ev, why


DEADLINES = FnCheck(
    "DL-01",
    "merchant deadlines met, with partial credit by stage",
    _deadlines,
    weight=2.0,
    hard=True,
    guards=("X5", "X15", "X29", "X32"),
)


def _deadline_credit(r: EpisodeRecord) -> Verdict:
    _, score, ev, why = _deadlines(r)
    return score >= 1.0, score, ev, why


DEADLINE_CREDIT = FnCheck(
    "DL-02",
    "share of deadline weight met (inquiry 1, chargeback and pre-arbitration 3)",
    _deadline_credit,
    weight=1.0,
    guards=("X5",),
)

__all__ = ["DEADLINES", "DEADLINE_CREDIT", "HARD_STAGES", "STAGE_WEIGHT", "Window", "windows"]
