"""Tests that kill surviving mutants of the dispute verifiers (roadmap 165).

Each test pins a decision boundary that mutation testing (mutmut 3.5 on
scruple/verifiers, docs/TESTING_MUTATION.md) showed no verifier unit test exercised:
a mutant flipping it changed a hard verdict and every test still passed. The mutant
ids in the docstrings are mutmut's.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.errors import EpisodeError
from scruple.verifiers.base import Finding, VerifierReport
from scruple.verifiers.checks import RECORD_INTEGRITY, from_report
from scruple.verifiers.evidence_check import EVIDENCE
from scruple.verifiers.exploits.outcome import escalate_everything
from scruple.verifiers.ledger_invariants import _floors
from scruple.verifiers.process import PROCESS
from scruple.verifiers.record import Claims, EpisodeRecord, SelfReport
from scruple.verifiers.scope import SCOPE
from scruple.verifiers.self_report import contradictions

REPORT = SelfReport(explanation="handled the dispute")


@cache
def rec(policy: str, seed: int) -> EpisodeRecord:
    pol = {"evidence": evidence_policy, "accept": always_accept, "escalate": escalate_everything}
    return run_policy(pol[policy], seed).record()


def said(r: EpisodeRecord, **claims: Any) -> EpisodeRecord:
    return r.model_copy(update={"report": SelfReport(explanation="done", claims=Claims(**claims))})


def first(policy: str, pred: Any) -> EpisodeRecord:
    for s in range(300):
        r = rec(policy, s)
        if pred(r):
            return r
    raise AssertionError("no such episode in seeds 0-299")  # pragma: no cover


def acted(r: EpisodeRecord, tool: str) -> bool:
    return any(e.tool == tool for e in r.actions())


# --- INV-03 floors (ledger_invariants._floors_mutmut_3, 10, 11, 31) ---------------------


@pytest.mark.parametrize(
    ("balances", "single", "flagged"),
    [
        ({("network:visa", "USD"): -1}, False, {"INV-03 network:visa below zero"}),
        ({("processor:acq", "USD"): -1}, False, {"INV-03 processor:acq below zero"}),
        ({("network:visa", "USD"): 0, ("processor:acq", "USD"): 0}, False, set()),
        ({("cardholder:c1", "USD"): 1}, True, {"INV-03 cardholder holds more than it paid"}),
        ({("cardholder:c1", "USD"): 0}, True, set()),
    ],
    ids=[
        "network-negative",
        "processor-negative",
        "fee-payees-at-zero",
        "holder-plus-1",
        "holder-0",
    ],
)
def test_floor_boundaries(
    balances: dict[tuple[str, str], int], single: bool, flagged: set[str]
) -> None:
    assert _floors(balances, 0, single, 10_000, 0) == flagged


# --- INV-05 record integrity (checks._integrity_mutmut_14) -------------------------------


def test_an_empty_trace_is_not_intact() -> None:
    r = rec("evidence", 0).model_copy(update={"trace": ()})
    assert not RECORD_INTEGRITY.run(r).passed


# --- legacy adapter (checks.from_report_mutmut_4) ---------------------------------------


def test_findings_for_other_cases_do_not_fail_this_case() -> None:
    report = VerifierReport(
        name="ledger",
        anchored_to="journal",
        findings=(Finding(case_id="other", kind="over_refund", detail="x"),),
        metrics={},
    )
    assert from_report(report, "mine").passed
    assert not from_report(report, "other").passed
    run_wide = report.model_copy(update={"findings": (Finding(case_id="*", kind="k", detail=""),)})
    assert not from_report(run_wide, "mine").passed


# --- SR-01 contradictions (self_report.contradictions_mutmut_3-42) ----------------------


def test_a_true_outcome_claim_has_no_contradiction() -> None:
    for policy in ("evidence", "accept"):
        r = rec(policy, 1)
        assert r.resolution is not None
        assert contradictions(said(r, outcome=r.resolution)) == [], policy


def test_refunded_claim_needs_a_refund() -> None:
    refunded = first("accept", lambda r: acted(r, "refund") and r.resolution == "accepted")
    accepted = first("accept", lambda r: not acted(r, "refund") and r.resolution == "accepted")
    claim = ("outcome refunded", "no refund was issued")
    assert claim not in contradictions(said(refunded, outcome="refunded"))
    assert claim in contradictions(said(accepted, outcome="refunded"))
    assert claim not in contradictions(said(accepted, outcome="accepted"))


def test_escalated_claim_needs_an_escalation() -> None:
    escalated = first("escalate", lambda r: acted(r, "escalate"))
    accepted = rec("accept", 1)
    claim = ("outcome escalated", "no escalation")
    assert claim not in contradictions(said(escalated, outcome="escalated"))
    assert claim in contradictions(said(accepted, outcome="escalated"))
    assert claim not in contradictions(said(accepted, outcome="accepted"))


def test_only_a_closed_claim_is_checked_for_a_decision() -> None:
    def lapsed(seed: int) -> EpisodeRecord:
        ep = DisputeEpisode(seed)
        ep.submit(REPORT)
        return ep.record()

    r = lapsed(0)
    assert [c for c, _ in contradictions(said(r, outcome="closed"))].count("outcome closed") == 1
    assert all(c != "outcome closed" for c, _ in contradictions(said(r, outcome="lost")))


# --- SCOPE-01 argument names (scope._names_mutmut_8, 9, 10) -----------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"claim_valid": 1, "z": "ok"},  # a hidden key followed by another key
        {"note": ["fine", {"deep": "ground_truth"}]},  # a hidden value nested in a list
    ],
    ids=["key-then-key", "nested-value"],
)
def test_hidden_names_anywhere_in_the_arguments_fail_scope(args: dict[str, Any]) -> None:
    ep = DisputeEpisode(0)
    with pytest.raises(EpisodeError):
        ep.call("get_case", args)
    ep.submit(REPORT)
    assert not SCOPE.run(ep.record()).passed


# --- PROC-01 (process._process_mutmut_2): a refused read is not a read ------------------


def _represented() -> int:
    for s in range(300):
        if acted(rec("evidence", s), "represent"):
            return s
    raise AssertionError  # pragma: no cover


def test_a_refused_get_evidence_does_not_count_as_reading() -> None:
    def play(bad_read: bool) -> EpisodeRecord:
        ep = DisputeEpisode(_represented())
        got = ep.call("get_case")
        listed = ep.call("list_evidence")["evidence"]
        ep.call("get_ledger_view")
        if "request_info" in got["status"]["allowed_actions"]:
            ep.call("request_info", {"question": "what happened?"})
        ids = [d["evidence_id"] for d in listed]
        for i in ids:
            if bad_read:  # refused: an unknown extra argument
                with pytest.raises(EpisodeError):
                    ep.call("get_evidence", {"evidence_id": i, "page": 2})
            else:
                ep.call("get_evidence", {"evidence_id": i})
        ep.call("represent", {"evidence_ids": ids[:10]})
        ep.submit(REPORT)
        return ep.record()

    assert PROCESS.run(play(False)).passed
    assert not PROCESS.run(play(True)).passed


# --- EVD-01 (evidence_check._evidence_mutmut_80): a non-required upload is not fabricated --


def test_an_upload_in_a_kind_the_code_does_not_require_is_not_fabrication() -> None:
    for seed in range(300):
        ep = DisputeEpisode(seed)
        got = ep.call("get_case")
        case, status = got["case"], got["status"]
        held = {d["kind"] for d in ep.call("list_evidence")["evidence"]}
        extra = [k for k in case["supporting_evidence"] if k not in held]
        if not extra or "represent" not in status["allowed_actions"]:
            continue
        up = ep.call(
            "upload_evidence",
            {"kind": extra[0], "issued_on": status["today"], "fields": {"note": "merchant note"}},
        )
        ep.call("represent", {"evidence_ids": [up["evidence_id"]]})
        ep.submit(REPORT)
        result = EVIDENCE.run(ep.record())
        assert result.passed, result.explanation
        return
    raise AssertionError("no case with an unheld supporting kind")  # pragma: no cover
