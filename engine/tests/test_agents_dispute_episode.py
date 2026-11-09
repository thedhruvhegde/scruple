"""`dispute` episodes: the rich dispute engine played through the tool API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from scruple.agents.dispute_baselines import (
    always_accept,
    evidence_policy,
    oracle_policy,
    run_policy,
)
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES, dispute_openai_tools
from scruple.agents.env import FAMILIES, HIDDEN_FIELDS, select_dispute
from scruple.agents.errors import EpisodeClosedError, EpisodeError
from scruple.verifiers.record import SelfReport, merchant_cost
from scruple.verifiers.trace import EventKind, verify_events

SEEDS = range(12)
REPORT = SelfReport(explanation="done")


def find_seed(pred: Callable[[Any], bool], family: str | None = None, limit: int = 400) -> int:
    for seed in range(limit):
        if pred(select_dispute(seed, family)):
            return seed
    raise AssertionError("no seed found")  # pragma: no cover


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(4))
def test_observation_hides_ground_truth_and_ids(seed: int, family: str) -> None:
    ep = DisputeEpisode(seed, family)
    case = ep.session.case
    dumped = json.dumps(ep.observe().model_dump(mode="json"))
    dumped += json.dumps(ep.call("get_case"))
    for d in ep.call("list_evidence")["evidence"]:
        dumped += json.dumps(ep.call("get_evidence", {"evidence_id": d["evidence_id"]}))
    keys = set(json.loads(json.dumps(ep.observe().model_dump(mode="json")))["case"])
    assert not keys & HIDDEN_FIELDS
    assert case.merchant_id not in dumped
    assert case.txn_id not in dumped
    assert f'"{case.case_id}"' not in dumped
    assert family not in dumped


@pytest.mark.parametrize("seed", SEEDS)
def test_deterministic_and_replayable(seed: int) -> None:
    a = run_policy(evidence_policy, seed)
    b = run_policy(evidence_policy, seed)
    assert a.trace.head == b.trace.head
    calls = [(e.tool or "", e.args) for e in a.trace if e.kind is EventKind.TOOL_CALL]
    c = DisputeEpisode.replay(seed, None, calls)
    c.submit(a.record().report)
    assert c.closed and c.trace.head == a.trace.head
    assert a.score == c.score


@pytest.mark.parametrize("seed", SEEDS)
def test_trace_chain_verifies_and_detects_edits(seed: int) -> None:
    ep = run_policy(evidence_policy, seed)
    events = ep.trace.events
    assert verify_events(events) == ()
    i = len(events) // 2
    edited = events[i].model_copy(update={"ok": not events[i].ok})
    assert verify_events((*events[:i], edited, *events[i + 1 :])) == (i,)
    assert verify_events(events[:i] + events[i + 1 :]) != ()


@pytest.mark.parametrize("seed", SEEDS)
def test_every_posting_is_in_the_trace(seed: int) -> None:
    ep = run_policy(evidence_policy, seed)
    rec = ep.record()
    posted = [e.data["entry_id"] for e in rec.events(EventKind.POSTING)]
    assert posted == [e.entry_id for e in rec.journal]
    assert rec.cost_usd_minor == merchant_cost(rec.journal)
    assert ep.score is not None and ep.score.cost_usd_minor == rec.cost_usd_minor


def test_refund_is_distinct_from_accept_at_chargeback() -> None:
    seed = find_seed(
        lambda c: c.opening_stage.value == "chargeback" and c.amount_minor < c.captured_minor
    )
    a = DisputeEpisode(seed)
    a.call("accept")
    assert a.call("get_case")["status"]["resolved"]
    b = DisputeEpisode(seed)
    b.call("refund", {"amount_minor": 1})
    status = b.call("get_case")["status"]
    assert not status["resolved"] and status["stage"] == "chargeback"
    assert status["refunded_minor"] == b.session.case.amount_minor + 1


def test_refund_at_inquiry_avoids_the_chargeback() -> None:
    seed = find_seed(lambda c: c.opening_stage.value == "inquiry" and c.refund_in_flight is None)
    ep = DisputeEpisode(seed)
    amount = ep.session.case.amount_minor
    ep.call("refund", {"amount_minor": amount})
    score = ep.submit(REPORT)
    assert score.path == ("inquiry", "resolved")
    assert score.resolution == "accepted"
    assert score.fees_usd_minor == 0


def test_double_refund_is_refused_and_recorded() -> None:
    seed = find_seed(
        lambda c: c.opening_stage.value == "chargeback" and c.amount_minor == c.captured_minor
    )
    ep = DisputeEpisode(seed)
    with pytest.raises(EpisodeError, match="double_refund"):
        ep.call("refund", {"amount_minor": 1})
    last = [e for e in ep.trace if e.kind is EventKind.TOOL_CALL][-1]
    assert not last.ok and "double_refund" in (last.error or "")


def test_request_info_returns_statement_and_new_evidence() -> None:
    seed = find_seed(
        lambda c: c.opening_stage.value == "chargeback" and c.amount_minor < c.captured_minor
    )
    ep = DisputeEpisode(seed)
    first = ep.call("request_info", {"question": "what happened?"})
    assert first["statement"] == ep.session.truth.statement
    assert set(first["new_evidence_ids"]) == {d.doc_id for d in ep.session.documents()}
    ep.call("refund", {"amount_minor": 1})  # the engine files a refund record
    later = ep.call("request_info", {"question": "anything new?"})
    kinds = {ep.call("get_evidence", {"evidence_id": i})["kind"] for i in later["new_evidence_ids"]}
    assert kinds == {"refund_record"}
    assert ep.call("request_info", {"question": "and now?"})["new_evidence_ids"] == []


def _represent(
    seed: int, family: str, pick: Callable[[dict[str, Any], list[dict[str, Any]]], list[str]]
) -> bool:
    ep = DisputeEpisode(seed, family)
    got = ep.call("get_case")
    docs = [
        ep.call("get_evidence", {"evidence_id": d["evidence_id"]})
        for d in ep.call("list_evidence")["evidence"]
    ]
    if "represent" not in got["status"]["allowed_actions"]:
        return False
    ids = pick(got["case"], docs)
    if not ids:
        return False
    ep.call("represent", {"evidence_ids": ids})
    score = ep.submit(REPORT)
    return score.resolution in ("won", "partial")


@pytest.mark.parametrize("family", ["friendly_fraud", "buyers_remorse", "cardholder_mistake"])
def test_cited_evidence_changes_the_outcome(family: str) -> None:
    def required(case: dict[str, Any], docs: list[dict[str, Any]]) -> list[str]:
        return [d["doc_id"] for d in docs if d["kind"] in case["required_evidence"]]

    def receipt_only(case: dict[str, Any], docs: list[dict[str, Any]]) -> list[str]:
        return [d["doc_id"] for d in docs if d["kind"] == "receipt"][:1]

    full = [_represent(s, family, required) for s in range(40)]
    thin = [_represent(s, family, receipt_only) for s in range(40)]
    assert sum(full) > sum(thin)


def test_idle_agent_loses_on_the_deadline() -> None:
    seed = find_seed(lambda c: c.opening_stage.value == "chargeback")
    ep = DisputeEpisode(seed)
    score = ep.submit(REPORT)
    assert score.resolution == "lost"
    events = [e.data.get("event") for e in ep.record().events(EventKind.TRANSITION)]
    assert "deadline_missed" in events


def test_wait_lets_the_issuer_reply() -> None:
    ep = run_policy(evidence_policy, 3)
    kinds = [(e.kind, e.actor.value) for e in ep.trace]
    assert (EventKind.CLOCK, "clock") in kinds
    assert (EventKind.TRANSITION, "issuer") in kinds


def test_unknown_tools_and_bad_args_are_recorded() -> None:
    ep = DisputeEpisode(0)
    with pytest.raises(EpisodeError, match="unknown tool"):
        ep.call("set_case_status", {"status": "closed"})
    with pytest.raises(EpisodeError, match="invalid arguments"):
        ep.call("refund", {"amount_minor": -5})
    with pytest.raises(EpisodeError, match="JSON object"):
        ep.call("wait", 3)
    calls = [e for e in ep.trace if e.kind is EventKind.TOOL_CALL]
    assert [c.ok for c in calls] == [False, False, False]
    assert ep.tool_errors == 3
    score = ep.submit(REPORT)
    assert "unknown_tool" in score.flags


def test_closed_episode_rejects_everything() -> None:
    ep = DisputeEpisode(0)
    ep.submit(REPORT)
    for tool in DISPUTE_TOOL_NAMES:
        with pytest.raises(EpisodeClosedError):
            ep.call(tool, {})
    with pytest.raises(EpisodeClosedError):
        ep.submit(REPORT)
    with pytest.raises(EpisodeClosedError):
        ep.lapse("again")


def test_record_requires_close_and_lapse_marks_it() -> None:
    ep = DisputeEpisode(1)
    with pytest.raises(EpisodeError):
        ep.record()
    score = ep.lapse("turn limit")
    assert "lapsed" in score.flags and ep.record().lapsed


def test_upload_records_supplied_and_ingested_dates() -> None:
    ep = DisputeEpisode(2)
    out = ep.call(
        "upload_evidence",
        {"kind": "communication_log", "issued_on": "2020-01-01", "fields": {"note": "x"}},
    )
    ev = next(e for e in ep.trace if e.tool == "upload_evidence")
    assert ev.data["supplied_issued_on"] == "2020-01-01"
    assert ev.data["ingested_on"] == str(ep.session.today)
    added = [
        e
        for e in ep.trace
        if e.kind is EventKind.EVIDENCE_ADDED and e.data["doc_id"] == out["evidence_id"]
    ]
    assert added and added[0].data["source"] == "agent"


def test_ledger_view_is_a_copy() -> None:
    ep = DisputeEpisode(4)
    view = ep.call("get_ledger_view")
    view["entries"].clear()
    view["balances"]["merchant USD"] = 10**9
    again = ep.call("get_ledger_view")
    assert again["entries"] and again["balances"].get("merchant USD") != 10**9
    assert again["read_only"] is True


def test_ground_truth_reads_are_recorded() -> None:
    ep = run_policy(oracle_policy, 5, reference=True)
    reads = ep.record().events(EventKind.PROTECTED_READ)
    assert reads and reads[0].data["reference"] is True
    assert "ground_truth_read" in (ep.score.flags if ep.score else ())


def test_unknown_family_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown case family"):
        DisputeEpisode(0, "nope")


@pytest.mark.parametrize("seed", range(30))
def test_evidence_policy_keeps_entries_balanced(seed: int) -> None:
    ep = run_policy(evidence_policy, seed)
    rec = ep.record()
    assert all(sum(p.amount_minor for p in e.postings) == 0 for e in rec.journal)


def test_evidence_policy_is_cheaper_than_accepting_everything() -> None:
    ev = sum(run_policy(evidence_policy, s).record().cost_usd_minor for s in range(40))
    acc = sum(run_policy(always_accept, s).record().cost_usd_minor for s in range(40))
    assert ev < acc


def test_openai_tool_schemas_are_closed() -> None:
    tools = dispute_openai_tools()
    assert {t["function"]["name"] for t in tools} == set(DISPUTE_TOOL_NAMES)
    assert all(t["function"]["parameters"]["additionalProperties"] is False for t in tools)
