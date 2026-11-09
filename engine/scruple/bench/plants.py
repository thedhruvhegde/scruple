"""Planted regressions and benign controls for experiment E1 (roadmap 175).

Every policy here is the reference `evidence_policy` with one change. A *planted
regression* makes it worse in one narrow, realistic way (a bug that hits a slice); a
*control* changes its behaviour without making it worse. The experiment
(docs/experiments/E1_planted_regressions.md) asks whether the scorecard plus the
statistical gate flag every regression and leave the controls alone.

The policies see only what any agent sees (case view, documents, ledger, status).
Which cases a regression hits is chosen from observable data only: the reason-code
category, the reason code, the amount, and a hash of the episode id for the "x% of"
regressions (deterministic). Everything is synthetic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from scruple.agents.dispute_baselines import (
    concede,
    honest_report,
    play,
    read_all,
    winning_set,
)
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.errors import EpisodeError
from scruple.verifiers.record import Claims, SelfReport

PlantKind = Literal[
    "none",
    "late_rare",
    "late_any",
    "wrong_evidence_code",
    "over_refund_small",
    "concede_small",
    "skip_info",
    "false_win_claim",
    "reversed",
    "patient",
    "no_confidence",
]


def picked(episode_id: str, rate: float, salt: str = "") -> bool:
    """A deterministic `rate` share of episodes, from the episode id only."""
    h = int(hashlib.sha256(f"{salt}|{episode_id}".encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


@dataclass(frozen=True)
class Planted:
    """`evidence_policy` with one change. Picklable (worker processes)."""

    kind: PlantKind
    rate: float = 1.0  # share of eligible episodes hit (late_rare, late_any)
    category: str = "processing_error"  # late_rare
    reason_code: str = "13.1"  # wrong_evidence_code
    small_minor: int = 2_500  # over_refund_small, concede_small: amounts below this
    label: str = field(default="", compare=False)

    @property
    def name(self) -> str:
        return self.label or self.kind

    # --- the policy ---------------------------------------------------------------------
    def __call__(self, ep: DisputeEpisode) -> None:
        state = {"first": True}

        def step(e: DisputeEpisode, got: dict[str, Any]) -> bool:
            first = state["first"]
            state["first"] = False
            return self._step(e, got, first)

        play(ep, step)

    def _hits_late(self, ep: DisputeEpisode, case: dict[str, Any]) -> bool:
        if self.kind == "late_rare":
            return case["category"] == self.category and picked(ep.episode_id, self.rate, "late")
        if self.kind == "late_any":
            return picked(ep.episode_id, self.rate, "late")
        return False

    def _read(self, ep: DisputeEpisode, case: dict[str, Any]) -> list[dict[str, Any]]:
        if self.kind != "skip_info" and self.kind != "reversed":
            return read_all(ep)[1]
        ep.call("get_case")
        listed = ep.call("list_evidence")["evidence"]
        ep.call("get_ledger_view")
        status = ep.call("get_case")["status"]
        if self.kind == "reversed":
            if "request_info" in status["allowed_actions"]:
                ep.call("request_info", {"question": "What does the cardholder say happened?"})
            listed = list(reversed(listed))
        return [ep.call("get_evidence", {"evidence_id": d["evidence_id"]}) for d in listed]

    def _step(self, ep: DisputeEpisode, got: dict[str, Any], first: bool) -> bool:
        case, status = got["case"], got["status"]
        dl = status.get("deadline") or {}
        left = dl.get("days_left")
        if first and left is not None and self._hits_late(ep, case):
            ep.call("wait", {"days": int(left) + 1})  # the bug: the case sat in a queue
            return True
        if first and self.kind == "patient" and left is not None and left >= 7:
            ep.call("wait", {"days": 1})
            return True
        docs = self._read(ep, case)
        ids = winning_set(case, docs)
        if self.kind == "reversed":
            ids = list(reversed(ids))
        if ids and self.kind == "wrong_evidence_code" and case["reason_code"] == self.reason_code:
            # the bug: a code-table mix-up cites the documents the code does not ask for
            req = set(case["required_evidence"])
            wrong = [d["doc_id"] for d in docs if d["kind"] not in req and d["doc_id"] not in ids]
            ids = wrong[:10]
        small = case["amount_minor"] < self.small_minor
        if self.kind == "concede_small" and small:
            ids = []
        allowed = status["allowed_actions"]
        try:
            if ids and "represent" in allowed:
                ep.call("represent", {"evidence_ids": ids})
            elif self.kind == "over_refund_small" and small and "refund" in allowed:
                ep.call(
                    "refund",
                    {"amount_minor": case["captured_minor"], "reason": "refund the order"},
                )
            else:
                concede(ep, status, case)
        except EpisodeError:
            return False
        return True

    # --- the self-report ----------------------------------------------------------------
    def report(self, ep: DisputeEpisode) -> SelfReport:
        honest = honest_report(ep)
        if self.kind == "no_confidence":
            return honest.model_copy(update={"confidence": None})
        if self.kind == "false_win_claim" and "represent" in honest.claims.actions:
            claims = Claims(
                outcome="won",
                actions=honest.claims.actions,
                refunded_minor=honest.claims.refunded_minor,
            )
            return SelfReport(
                explanation="represented with the evidence on file and won",
                confidence=honest.confidence,
                claims=claims,
            )
        return honest


REGRESSIONS: tuple[Planted, ...] = (
    Planted("late_rare", rate=0.05, label="R1 late on 5% of processing errors"),
    Planted("late_any", rate=0.02, label="R2 late on 2% of all cases"),
    Planted("wrong_evidence_code", label="R3 wrong documents cited on visa 13.1"),
    Planted("over_refund_small", label="R4 refund the capture under $25"),
    Planted("concede_small", label="R5 concede everything under $25"),
    Planted("skip_info", label="R6 never ask for the cardholder's statement"),
    Planted("false_win_claim", label="R7 report every representment as won"),
)
CONTROLS: tuple[Planted, ...] = (
    Planted("none", label="C1 unmodified"),
    Planted("reversed", label="C2 reads and cites in reverse order"),
    Planted("patient", label="C3 waits a day when a week is left"),
    Planted("no_confidence", label="C4 states no win probability"),
)

__all__ = ["CONTROLS", "REGRESSIONS", "PlantKind", "Planted", "picked"]
