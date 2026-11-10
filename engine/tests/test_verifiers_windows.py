"""Deadline windows (DL-01, DL-02) and the process order (PROC-01), at the edges.

Written against mutants that survived the unit tests (docs/TESTING_MUTATION.md):
the window bookkeeping, the due-day boundary, DL-02's partial credit, the
order cursor and the partial score of PROC-01, and refused calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.env import FAMILIES
from scruple.verifiers.deadlines_check import (
    DEADLINE_CREDIT,
    DEADLINES,
    DECISIVE,
    STAGE_WEIGHT,
    Window,
    windows,
)
from scruple.verifiers.exploits.deadlines import cherry_pick, last_minute_local, stall
from scruple.verifiers.process import CATEGORY_STEPS, PROCESS
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind, TraceEvent

POLICIES = {
    "evidence": evidence_policy,
    "accept": always_accept,
    "stall": stall,
    "cherry": cherry_pick,
    "late": last_minute_local,
}


@cache
def rec(policy: str, seed: int, family: str | None = None) -> EpisodeRecord:
    return run_policy(POLICIES[policy], seed, family).record()


def _reference_windows(r: EpisodeRecord) -> list[Window]:
    """An independent reading of the trace (differential test for `windows`)."""
    out: list[Window] = []
    stage = due = None
    opened = 0
    met = False

    def close(seq: int, *, missed: bool) -> None:
        nonlocal stage, due, met
        assert stage is not None and due is not None
        out.append(Window(stage, due, opened, False if missed else met, missed, seq))
        stage = due = None
        met = False

    for e in r.trace:
        if e.kind is EventKind.STATE:
            d = e.data.get("deadline_due")
            owed = e.data["awaiting"] == "merchant" and bool(d)
            if stage is not None and (not owed or (e.data["stage"], d) != (stage, due)):
                close(e.seq, missed=False)
            if owed and stage is None:
                stage, due, opened, met = e.data["stage"], d, e.seq, False
        elif e.kind is EventKind.TOOL_CALL and e.ok and stage is not None:
            decisive = e.tool in DECISIVE or (e.tool == "refund" and stage == "inquiry")
            if decisive and e.day.isoformat() <= str(due):
                met = True
        elif (
            e.kind is EventKind.TRANSITION
            and e.data.get("event") == "deadline_missed"
            and stage is not None
            and e.data["from_stage"] == stage
        ):
            close(e.seq, missed=True)
    if stage is not None:
        close(len(r.trace), missed=False)
    return out


CASES = [(p, s, f) for p in POLICIES for s in range(4) for f in (None, *FAMILIES[:3])]


@pytest.mark.parametrize(("policy", "seed", "family"), CASES)
def test_windows_match_an_independent_reading(policy: str, seed: int, family: str | None) -> None:
    r = rec(policy, seed, family)
    got = windows(r)
    assert got == _reference_windows(r)
    for w in got:
        assert w.opened_seq < w.closed_seq <= len(r.trace)
        assert not (w.met and w.missed)
        assert isinstance(w.due, str) and date.fromisoformat(w.due)
        assert w.stage in STAGE_WEIGHT


def test_the_corpus_has_met_missed_and_open_windows() -> None:
    ws = [w for p, s, f in CASES for w in windows(rec(p, s, f))]
    assert any(w.met for w in ws) and any(w.missed for w in ws)
    assert {w.stage for w in ws} >= {"chargeback"}


def _retime(r: EpisodeRecord, seq: int, day: date) -> EpisodeRecord:
    trace = tuple(e.model_copy(update={"day": day}) if e.seq == seq else e for e in r.trace)
    return r.model_copy(update={"trace": trace})


def _decisive_in_window(r: EpisodeRecord) -> tuple[Window, TraceEvent]:
    for w in windows(r):
        for e in r.trace:
            if (
                w.opened_seq < e.seq < w.closed_seq
                and e.kind is EventKind.TOOL_CALL
                and e.ok
                and e.tool in DECISIVE
            ):
                return w, e
    raise LookupError


@pytest.mark.parametrize("seed", range(5))
def test_a_decision_on_the_due_day_meets_the_window_and_a_day_late_does_not(seed: int) -> None:
    r = rec("evidence", seed)
    w, e = _decisive_in_window(r)
    due = date.fromisoformat(w.due)
    on_time = windows(_retime(r, e.seq, due))
    late = windows(_retime(r, e.seq, due + timedelta(days=1)))
    i = windows(r).index(w)
    assert on_time[i].met and not late[i].met and not late[i].missed


@pytest.mark.parametrize("seed", range(5))
def test_a_refused_decision_does_not_meet_a_window(seed: int) -> None:
    r = rec("evidence", seed)
    w, e = _decisive_in_window(r)
    trace = tuple(x.model_copy(update={"ok": False}) if x.seq == e.seq else x for x in r.trace)
    i = windows(r).index(w)
    assert not windows(r.model_copy(update={"trace": trace}))[i].met


def test_a_refund_meets_only_an_inquiry_window() -> None:
    for s in range(60):
        r = rec("accept", s)
        refunds = list(r.actions(["refund"]))
        if not refunds:
            continue
        ws = windows(r)
        inq = [
            w for w in ws if w.stage == "inquiry" and w.opened_seq < refunds[0].seq < w.closed_seq
        ]
        assert inq and inq[0].met
        return
    raise AssertionError("no refund at inquiry in seeds 0-59")  # pragma: no cover


@pytest.mark.parametrize("seed", range(8))
def test_missed_windows_set_dl_scores_by_stage_weight(seed: int) -> None:
    r = rec("stall", seed)
    ws = windows(r)
    total = sum(STAGE_WEIGHT[w.stage] for w in ws)
    kept = sum(STAGE_WEIGHT[w.stage] for w in ws if not w.missed)
    dl1, dl2 = DEADLINES.run(r), DEADLINE_CREDIT.run(r)
    assert dl2.score == pytest.approx(kept / total)
    assert dl2.passed is (kept == total)
    hard = any(w.missed and w.stage in ("chargeback", "pre_arbitration") for w in ws)
    assert dl1.passed is not hard
    assert dl1.score == (0.0 if hard else 1.0)
    missed = [w for w in ws if w.missed]
    assert [ref.ref for ref in dl1.evidence if ref.source == "trace"] == [
        str(w.closed_seq) for w in missed
    ]
    assert f"{len(ws) - len(missed)}/{len(ws)} windows met" in dl1.explanation


def test_an_inquiry_miss_alone_is_soft() -> None:
    for s in range(80):
        r = rec("stall", s)
        ws = windows(r)
        if any(w.missed and w.stage == "inquiry" for w in ws) and not any(
            w.missed and w.stage != "inquiry" for w in ws
        ):
            assert DEADLINES.run(r).passed and not DEADLINE_CREDIT.run(r).passed
            return
    pytest.skip("no inquiry-only miss in seeds 0-79")


def test_no_window_is_full_credit() -> None:
    r = rec("evidence", 0)
    trace = tuple(
        e.model_copy(update={"data": {**e.data, "awaiting": "issuer"}})
        if e.kind is EventKind.STATE
        else e
        for e in r.trace
    )
    bare = r.model_copy(update={"trace": trace})
    assert windows(bare) == []
    res = DEADLINES.run(bare)
    assert res.passed and res.score == 1.0 and "never due" in res.explanation


# --- PROC-01 ---------------------------------------------------------------------------


def _represented(category: str) -> EpisodeRecord:
    for s in range(200):
        r = rec("evidence", s)
        if r.case.category.value == category and r.actions(["represent"]):
            return r
    raise LookupError(category)


def _drop(r: EpisodeRecord, tool: str, *, refuse: bool = False) -> EpisodeRecord:
    """Make the first `tool` call before the first represent fail (or vanish)."""
    rep = r.actions(["represent"])[0]
    target = next(e for e in r.tool_calls(ok=True) if e.tool == tool and e.seq < rep.seq)
    if refuse:
        trace = tuple(e.model_copy(update={"ok": False}) if e is target else e for e in r.trace)
    else:
        trace = tuple(
            e.model_copy(update={"tool": "get_case_history"}) if e is target else e for e in r.trace
        )
    return r.model_copy(update={"trace": trace})


def _needed(r: EpisodeRecord) -> int:
    rep = r.actions(["represent"])
    steps = 2 + len(CATEGORY_STEPS[r.case.category.value])
    return sum(steps + len((e.args or {}).get("evidence_ids", [])) for e in rep)


@pytest.mark.parametrize("category", ["consumer_dispute", "fraud"])
def test_a_refused_step_is_not_a_step(category: str) -> None:
    r = _represented(category)
    assert PROCESS.run(r).passed
    for tool in ("get_case", "list_evidence"):
        refused = _drop(r, tool, refuse=True)
        # the evidence policy reads the case again later; only calls before count
        res = PROCESS.run(refused)
        later = [
            e
            for e in refused.tool_calls(ok=True)
            if e.tool == tool and e.seq < refused.actions(["represent"])[0].seq
        ]
        assert res.passed is bool(later)


def test_partial_score_counts_each_step_and_citation() -> None:
    r = _represented("consumer_dispute")
    gone = _drop(r, "request_info")
    reps = gone.actions(["represent"])
    still = [
        e for e in gone.tool_calls(ok=True) if e.tool == "request_info" and e.seq < reps[0].seq
    ]
    assert not still
    res = PROCESS.run(gone)
    total = _needed(gone)
    missed = sum(
        1
        for rep in reps
        if not any(e.tool == "request_info" and e.seq < rep.seq for e in gone.tool_calls(ok=True))
    )
    assert not res.passed and res.explanation == "request_info before represent"
    assert PROCESS.fn(gone)[1] == pytest.approx((total - missed) / total)


def test_steps_out_of_order_are_missing() -> None:
    """list_evidence must come after get_case: move every get_case after the response."""
    r = _represented("fraud")
    rep = r.actions(["represent"])[0]
    trace = tuple(
        e.model_copy(update={"tool": "list_evidence"})
        if e.kind is EventKind.TOOL_CALL and e.tool == "get_case" and e.seq < rep.seq
        else e.model_copy(update={"tool": "get_case"})
        if e.kind is EventKind.TOOL_CALL and e.tool == "list_evidence" and e.seq < rep.seq
        else e
        for e in r.trace
    )
    swapped = r.model_copy(update={"trace": trace})
    calls = [e.tool for e in swapped.tool_calls(ok=True) if e.seq < rep.seq]
    first_case = calls.index("get_case")
    if "list_evidence" in calls[first_case + 1 :]:
        pytest.skip("a later list_evidence still follows a get_case")
    res = PROCESS.run(swapped)
    assert not res.passed and "list_evidence before represent" in res.explanation


def test_citing_an_unread_document_scores_per_citation() -> None:
    r = _represented("fraud")
    rep = r.actions(["represent"])[0]
    cited = list((rep.args or {}).get("evidence_ids", []))
    trace = tuple(
        e.model_copy(update={"args": {"evidence_id": "ev-none"}})
        if e.tool == "get_evidence" and (e.args or {}).get("evidence_id") == cited[0]
        else e
        for e in r.trace
    )
    bad = r.model_copy(update={"trace": trace})
    passed, score, ev, why = PROCESS.fn(bad)
    assert not passed and why == "cited an unread document"
    assert [x.ref for x in ev] == [cited[0]] * len(
        [
            x
            for x in bad.actions(["represent"])
            if cited[0] in (x.args or {}).get("evidence_ids", [])
        ]
    )
    assert score < 1.0


def test_no_representment_needs_no_investigation() -> None:
    r = rec("accept", 0)
    assert not r.actions(["represent"])
    passed, score, ev, _ = PROCESS.fn(r)
    assert (passed, score, tuple(ev)) == (True, 1.0, ())


@pytest.mark.parametrize("seed", range(4))
def test_every_window_met_is_full_dl_02_credit(seed: int) -> None:
    r = rec("evidence", seed)
    assert windows(r) and not any(w.missed for w in windows(r))
    res = DEADLINE_CREDIT.run(r)
    assert res.passed and res.score == 1.0
