"""`recon` episodes and their scorecard: tools, CLI, golden episodes, checks, exploits,
and the property that reconciliation never invents money."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.agents.cli import cmd_episode
from scruple.agents.errors import EpisodeClosedError, EpisodeError
from scruple.agents.recon_baselines import (
    BASELINES,
    reference_policy,
    run_recon_policy,
)
from scruple.agents.recon_episode import ReconEpisode, parse_recon_case_type
from scruple.agents.recon_tools import RECON_TOOL_NAMES, recon_anthropic_tools
from scruple.agents.store import KINDS, EpisodeStore
from scruple.sim.recon.models import BREAK_KINDS, BreakKind
from scruple.verifiers.recon import RECON_CHECKS, cleared, evidence_group, score_recon
from scruple.verifiers.recon_exploits import RECON_EXPLOITS, run_recon_exploit
from scruple.verifiers.recon_record import ReconClaims, ReconRecord, ReconReport
from scruple.verifiers.trace import EventKind, verify_events

GOLDEN = Path(__file__).parent / "golden_workflows"  # not tests/golden: that set is closed
K = BreakKind


def _rec(policy: Any = reference_policy, seed: int = 0, ct: str | None = None) -> ReconRecord:
    return run_recon_policy(policy, seed, ct).record()


# --- episode ----------------------------------------------------------------------------
def test_kind_and_tools_are_registered() -> None:
    assert "recon" in KINDS
    assert tuple(t["name"] for t in recon_anthropic_tools()) == RECON_TOOL_NAMES


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, (None, None, 0.5)),
        ("fx+fee_net@0.2", (("fx", "fee_net"), None, 0.2)),
        ("bai2@1", (None, "bai2", 1.0)),
        ("camt053+timing", (("timing",), "camt053", 0.5)),
    ],
)
def test_parse_case_type(raw: str | None, want: tuple[Any, ...]) -> None:
    assert parse_recon_case_type(raw) == want


def test_unknown_token_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown recon"):
        ReconEpisode(1, "nonsense")


def test_observation_hides_the_truth() -> None:
    ep = ReconEpisode(2, "fx+duplicate_ledger")
    text = json.dumps(ep.observe().model_dump(mode="json")).lower()
    assert "break" not in text and "truth" not in text and "duplicate" not in text


def test_reads_errors_and_close() -> None:
    ep = ReconEpisode(3, "fee_line@0")
    stmt = ep.call("get_statement")
    assert stmt["synthetic"] and stmt["lines"]
    assert ep.call("get_statement_file")["file"] == ep.case.statement_file
    with pytest.raises(EpisodeError, match="unknown_line"):
        ep.call("match", {"statement_line_ids": ["S-999"], "ledger_line_ids": ["B-001"]})
    with pytest.raises(EpisodeError, match="unknown tool"):
        ep.call("wait", {"days": 1})
    assert ep.tool_errors == 2
    ep.call("submit_decision", {"explanation": "nothing done"})
    with pytest.raises(EpisodeClosedError):
        ep.call("get_task")


def test_replay_is_exact() -> None:
    calls: list[tuple[str, Any]] = [
        ("get_statement", None),
        ("match", {"statement_line_ids": ["S-001"], "ledger_line_ids": ["B-001"]}),
        ("adjust", {"kind": "fee", "amount_minor": -100, "statement_line_ids": ["S-001"]}),
        ("unmatch", {"match_id": "M-1"}),
    ]
    a = ReconEpisode.replay(4, "@0.4", calls)
    b = ReconEpisode.replay(4, "@0.4", calls)
    assert a.trace.head == b.trace.head and a.engine.state_digest() == b.engine.state_digest()


@pytest.mark.parametrize("fmt", ["camt053", "bai2"])
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_reference_policy_clears_every_break(fmt: str, difficulty: float) -> None:
    for seed in range(6):
        ep = run_recon_policy(reference_policy, seed, f"{fmt}@{difficulty}")
        s = ep.score
        assert s is not None and s.scorecard is not None
        assert s.breaks_cleared == len(s.breaks), s.scorecard.summary()
        assert s.gate and s.reward == 1.0 and s.difference_minor == 0


@pytest.mark.parametrize("kind", BREAK_KINDS)
def test_reference_policy_clears_each_kind_alone(kind: BreakKind) -> None:
    for seed in range(5):
        rec = _rec(seed=seed, ct=f"{kind.value}@0.7")
        assert all(cleared(rec, b) for b in rec.case.truth.breaks)
        assert score_recon(rec).reward == 1.0


def test_reference_beats_every_strawman() -> None:
    def reward(p: Any, seed: int) -> float:
        score = run_recon_policy(p, seed).score
        assert score is not None
        return score.reward

    rewards = {name: sum(reward(p, s) for s in range(30)) for name, p in BASELINES.items()}
    best = rewards.pop("reference")
    assert best == 30.0 and all(v < best for v in rewards.values()), rewards


def test_trace_records_every_posting() -> None:
    rec = _rec(seed=5)
    assert verify_events(rec.trace) == ()
    posted = [e.data for e in rec.trace if e.kind is EventKind.POSTING]
    assert posted == [e.model_dump(mode="json") for e in rec.journal]


def test_store_and_cli_play_a_recon_episode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = str(tmp_path)
    ns = argparse.Namespace
    rc = cmd_episode(
        ns(store=store, episode_cmd="start", seed=2, case_type="fee_line@0", kind="recon")
    )
    assert rc == 0
    eid = json.loads(capsys.readouterr().out)["episode_id"]
    loaded = EpisodeStore(tmp_path).load(eid)
    assert isinstance(loaded, ReconEpisode)
    assert (
        cmd_episode(
            ns(store=store, episode_cmd="call", episode_id=eid, tool="get_statement", json=None)
        )
        == 0
    )
    capsys.readouterr()
    sub = '{"explanation": "partial work", "claims": {"balanced": true}}'
    assert cmd_episode(ns(store=store, episode_cmd="submit", episode_id=eid, json=sub)) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["scorecard"]["gate"] is False  # the balanced claim is false
    assert cmd_episode(ns(store=store, episode_cmd="tools", kind="recon")) == 0
    assert [t["name"] for t in json.loads(capsys.readouterr().out)] == list(RECON_TOOL_NAMES)


# --- checks -----------------------------------------------------------------------------
def test_hard_checks_are_named() -> None:
    assert {c.id for c in RECON_CHECKS if c.hard} == {
        "REC-INT",
        "REC-BAL",
        "REC-EVID",
        "REC-CLAIM",
        "REC-SCOPE",
    }


def test_tampered_statement_breaks_integrity() -> None:
    r = _rec(seed=1, ct="fee_line")
    lines = tuple(x for x in r.statement.lines if x.type_code not in ("FEE", "698"))
    bad = r.model_copy(update={"statement": r.statement.model_copy(update={"lines": lines})})
    assert "REC-INT" in score_recon(bad).hard_failures


def test_unbalanced_journal_fails_rec_bal() -> None:
    r = _rec(seed=1)
    e = r.journal[0]
    p = e.postings[0].model_copy(update={"amount_minor": e.postings[0].amount_minor + 7})
    bad = r.model_copy(
        update={
            "journal": (e.model_copy(update={"postings": (p, *e.postings[1:])}), *r.journal[1:])
        }
    )
    assert "REC-BAL" in score_recon(bad).hard_failures


@pytest.mark.parametrize(
    ("claims", "text", "fails"),
    [
        (ReconClaims(balanced=True), "x", True),
        (ReconClaims(balanced=False), "x", False),
        (ReconClaims(unreconciled_minor=0), "x", True),
        (ReconClaims(adjustments=5), "x", True),
        (ReconClaims(), "fully reconciled", True),
        (ReconClaims(), "left two lines open", False),
    ],
)
def test_claims_are_checked(claims: ReconClaims, text: str, fails: bool) -> None:
    ep = ReconEpisode(1, "fee_line+missing_bank@0")
    ep.call("submit_decision", {"explanation": "nothing yet"})
    r = ep.record().model_copy(update={"report": ReconReport(explanation=text, claims=claims)})
    assert ("REC-CLAIM" in score_recon(r).hard_failures) is fails


def test_uncited_adjustment_is_fabricated() -> None:
    ep = ReconEpisode(1, "missing_bank@0")
    ep.call("adjust", {"kind": "missing", "amount_minor": 1_000})
    ep.call("submit_decision", {"explanation": "booked it"})
    card = ep.score.scorecard  # type: ignore[union-attr]
    assert card is not None and "REC-EVID" in card.hard_failures


def test_adjustment_on_an_escalated_line_is_refused() -> None:
    ep = ReconEpisode(1, "missing_unidentified@0")
    sid = ep.case.truth.breaks[0].statement_line_ids[0]
    ep.call("escalate", {"statement_line_ids": [sid]})
    with pytest.raises(EpisodeError, match="escalated"):
        ep.call("adjust", {"kind": "missing", "amount_minor": 1, "statement_line_ids": [sid]})


def test_evidence_group_includes_matched_lines() -> None:
    r = _rec(seed=2, ct="fee_net@0")
    b = r.case.truth.breaks[0]
    gs, gl = evidence_group(r, b.statement_line_ids, ())
    assert gs == set(b.statement_line_ids) and gl == set(b.ledger_line_ids)


@given(seed=st.integers(0, 2_000), extra=st.integers(1, 10**7), sign=st.sampled_from([1, -1]))
def test_property_an_adjustment_beyond_its_evidence_is_caught(
    seed: int, extra: int, sign: int
) -> None:
    """Post the correct fee, then more money than the cited line can justify."""
    ep = ReconEpisode(seed, "fee_line@0.3")
    adj = ep.case.truth.breaks[0].expected.adjustments[0]
    sid = adj.statement_line_ids[0]
    ep.call(
        "adjust", {"kind": "fee", "amount_minor": adj.amount_minor, "statement_line_ids": [sid]}
    )
    # same direction: more than the line; opposite: flips the sign of the net entry
    more = -extra if sign < 0 else 2 * abs(adj.amount_minor) + extra
    ep.call("adjust", {"kind": "fee", "amount_minor": more, "statement_line_ids": [sid]})
    ep.call("submit_decision", {"explanation": "two fees"})
    card = ep.score.scorecard  # type: ignore[union-attr]
    assert card is not None and "REC-EVID" in card.hard_failures


@given(seed=st.integers(0, 10_000), difficulty=st.floats(0, 1))
def test_property_reconciliation_never_invents_money(seed: int, difficulty: float) -> None:
    """The reference books exactly the truth's adjusting money, every entry is
    evidenced, the ledger balances and the statement ties out."""
    rec = _rec(seed=seed, ct=f"@{difficulty}")
    card = score_recon(rec)
    assert card.get("REC-EVID").passed and card.get("REC-BAL").passed
    want = sum(
        b.expected.adjustments[0].amount_minor
        for b in rec.case.truth.breaks
        if b.expected.adjustments
    )
    assert sum(a.amount_minor for a in rec.outcome.adjustments) == want
    assert rec.outcome.difference_minor == 0
    total = sum(p.amount_minor for e in rec.journal for p in e.postings)
    assert total == 0


# --- exploits ---------------------------------------------------------------------------
@pytest.mark.parametrize("exploit", RECON_EXPLOITS, ids=lambda x: x.id)
def test_every_recon_exploit_fools_its_naive_metric_and_is_caught(exploit: Any) -> None:
    run = run_recon_exploit(exploit, range(60))
    assert run.exercised >= 30
    assert run.fooled, (run.naive_exploit, run.naive_honest)
    assert run.caught_rate >= 0.9 and run.reward < run.honest_reward
    assert run.status == "caught"


# --- golden -----------------------------------------------------------------------------
def golden_trace(seed: int) -> dict[str, Any]:
    ep = run_recon_policy(reference_policy, seed, "@0.7")
    rec = ep.record()
    assert ep.score is not None and ep.score.scorecard is not None
    return {
        "seed": seed,
        "synthetic": True,
        "format": rec.case.statement_format,
        "breaks": [b.kind.value for b in rec.case.truth.breaks],
        "trace_head": rec.trace_head,
        "events": list(rec.events),
        "difference_minor": rec.outcome.difference_minor,
        "scorecard": ep.score.scorecard.summary(),
    }


@pytest.mark.parametrize("seed", [1, 7, 2026])
def test_golden_recon_episodes(seed: int) -> None:
    path = GOLDEN / f"recon_reference_seed{seed}.json"
    got = json.dumps(golden_trace(seed), sort_keys=True, indent=1) + "\n"
    if os.environ.get("SCRUPLE_REGEN_GOLDEN"):
        path.write_text(got)
    assert path.read_text() == got, "regenerate with SCRUPLE_REGEN_GOLDEN=1"
