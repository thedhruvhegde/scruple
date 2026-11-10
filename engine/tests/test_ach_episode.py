"""`ach` episodes: tools, trace, replay, the CLI store, golden episodes, reference policy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scruple.agents.ach_baselines import (
    BASELINES,
    honest_report,
    reference_policy,
    run_ach_policy,
)
from scruple.agents.ach_episode import AchEpisode, parse_case_type
from scruple.agents.ach_tools import ACH_TOOL_NAMES, ach_anthropic_tools
from scruple.agents.cli import cmd_episode
from scruple.agents.errors import EpisodeClosedError, EpisodeError, ToolArgumentError
from scruple.agents.store import KINDS, EpisodeStore
from scruple.sim.ach.models import FAMILIES, AchFamily
from scruple.verifiers.trace import EventKind, verify_events

GOLDEN = Path(__file__).parent / "golden_workflows"  # not tests/golden: that set is closed
GOLDEN_SEEDS = (1, 7, 2026)


def test_kind_is_registered() -> None:
    assert "ach" in KINDS
    names = [t["name"] for t in ach_anthropic_tools()]
    assert tuple(names) == ACH_TOOL_NAMES
    assert all(t["input_schema"]["type"] == "object" for t in ach_anthropic_tools())


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, (None, 0.5)),
        ("unauthorized", ("unauthorized", 0.5)),
        ("unauthorized@0.9", ("unauthorized", 0.9)),
        ("@0", (None, 0.0)),
    ],
)
def test_parse_case_type(raw: str | None, want: tuple[str | None, float]) -> None:
    assert parse_case_type(raw) == want


@pytest.mark.parametrize("raw", ["x@abc", "x@2"])
def test_parse_case_type_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_case_type(raw)


def test_unknown_family_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown ach family"):
        AchEpisode(1, "nope")


def test_observation_hides_the_truth() -> None:
    for fam in FAMILIES:
        ep = AchEpisode(3, fam.value)
        text = json.dumps(ep.observe().model_dump(mode="json")).lower()
        assert fam.value not in text and "funds_on" not in text and "truth" not in text
        assert ep.observe().synthetic


def test_refused_and_unknown_calls_are_traced_as_errors() -> None:
    ep = AchEpisode(2, "account_closed@0")
    with pytest.raises(EpisodeError, match="unknown tool"):
        ep.call("refund")
    with pytest.raises(ToolArgumentError):
        ep.call("dishonor", {"return_id": "RET-1", "code": "R99"})
    with pytest.raises(EpisodeError, match="unknown return"):
        ep.call("get_return", {"return_id": "RET-7"})
    ep.call("write_off")
    with pytest.raises(EpisodeError, match="nothing_open"):
        ep.call("accept")
    assert ep.tool_errors == 4
    errors = [e for e in ep.trace if e.kind is EventKind.TOOL_CALL and not e.ok]
    assert len(errors) == 4
    ep.call("submit_decision", {"explanation": "wrote it off"})
    with pytest.raises(EpisodeClosedError):
        ep.call("get_case")


def test_wait_moves_the_clock_one_day_at_a_time() -> None:
    ep = AchEpisode(4, "duplicate_return@0")
    start = ep.today
    out = ep.call("wait", {"days": 6})
    assert out["status"]["open_receivable_minor"] == 2 * ep.case.amount_minor
    assert (ep.today - start).days == 6
    assert any(e.kind is EventKind.CLOCK for e in ep.trace)


def test_replay_rebuilds_the_episode_exactly() -> None:
    calls: list[tuple[str, Any]] = [
        ("get_case", None),
        ("wait", {"days": 5}),
        ("dishonor", {"return_id": "RET-2", "code": "R67"}),
        ("accept", None),  # refused: pending
        ("wait", {"days": 6}),
        ("write_off", {}),
    ]
    a = AchEpisode.replay(9, "duplicate_return", calls)
    b = AchEpisode.replay(9, "duplicate_return", calls)
    assert a.trace.head == b.trace.head
    assert a.engine.state_digest() == b.engine.state_digest()
    assert a.tool_errors == 1


def test_trace_is_chained_and_records_every_posting() -> None:
    ep = run_ach_policy(reference_policy, 11)
    rec = ep.record()
    assert verify_events(rec.trace) == ()
    posted = [e.data for e in rec.trace if e.kind is EventKind.POSTING]
    assert posted == [e.model_dump(mode="json") for e in rec.journal]


def test_protected_read_is_flagged_and_fails_scope() -> None:
    ep = AchEpisode(5, "unauthorized")
    ep.ground_truth_for_reference()
    ep.call("accept")
    score = ep.call("submit_decision", {"explanation": "peeked"})
    assert "ground_truth_read" in score["flags"]
    assert ep.score is not None and "ACH-SCOPE" in ep.score.scorecard.hard_failures  # type: ignore[union-attr]
    ref = AchEpisode(5, "unauthorized", reference=True)
    ref.ground_truth_for_reference()
    ref.call("accept")
    ref.submit(honest_report(ref))
    assert ref.score is not None and ref.score.gate


def test_lapse_closes_with_flags() -> None:
    ep = AchEpisode(6)
    s = ep.lapse("turn limit")
    assert "lapsed" in s.flags and ep.closed
    assert ep.record().lapsed
    with pytest.raises(EpisodeClosedError):
        ep.lapse("again")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_reference_policy_is_correct_and_gated(family: AchFamily, difficulty: float) -> None:
    for seed in range(8):
        ep = run_ach_policy(reference_policy, seed, f"{family.value}@{difficulty}")
        s = ep.score
        assert s is not None
        assert s.correct, (seed, s.allocation, ep.case.truth.expected)
        assert s.gate and s.reward == 1.0, s.scorecard.summary()  # type: ignore[union-attr]
        assert not s.violations


def test_reference_beats_every_strawman() -> None:
    def reward(p: Any, seed: int) -> float:
        score = run_ach_policy(p, seed).score
        assert score is not None
        return score.reward

    rewards = {name: sum(reward(p, s) for s in range(60)) for name, p in BASELINES.items()}
    best = rewards.pop("reference")
    assert best == 60.0
    assert all(v < best for v in rewards.values()), rewards


# --- CLI store --------------------------------------------------------------------------
def _Args(**kw: Any) -> argparse.Namespace:  # noqa: N802 - reads like the namespace it builds
    return argparse.Namespace(**kw)


def test_cli_plays_an_ach_episode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = str(tmp_path)
    assert (
        cmd_episode(
            _Args(
                store=store, episode_cmd="start", seed=3, case_type="unauthorized@0.2", kind="ach"
            )
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    eid = started["episode_id"]
    assert started["observation"]["kind"] == "ach"
    assert (
        cmd_episode(
            _Args(store=store, episode_cmd="call", episode_id=eid, tool="accept", json="{}")
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cmd_episode(
            _Args(store=store, episode_cmd="call", episode_id=eid, tool="accept", json="{}")
        )
        == 1
    )
    assert "error" in json.loads(capsys.readouterr().out)
    assert (
        cmd_episode(
            _Args(store=store, episode_cmd="submit", episode_id=eid, json='{"explanation": "R29"}')
        )
        == 0
    )
    card = json.loads(capsys.readouterr().out)
    assert card["correct"] and card["family"] == "unauthorized"
    assert cmd_episode(_Args(store=store, episode_cmd="show", episode_id=eid)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "ach" and shown["closed"] and shown["action"] == "accept"
    assert len(shown["calls"]) == 3
    assert cmd_episode(_Args(store=store, episode_cmd="tools", kind="ach")) == 0
    tools = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in tools] == list(ACH_TOOL_NAMES)


def test_store_reloads_an_ach_episode(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(8, "nsf_recoverable@0", "ach")
    store.call(ep.episode_id, "wait", {"days": 30})
    again = store.load(ep.episode_id)
    assert isinstance(again, AchEpisode)
    assert (
        again.trace.head
        == AchEpisode.replay(8, "nsf_recoverable@0", [("wait", {"days": 30})]).trace.head
    )


# --- golden episodes --------------------------------------------------------------------
def golden_trace(seed: int) -> dict[str, Any]:
    ep = run_ach_policy(reference_policy, seed, "@0.7")
    rec = ep.record()
    assert ep.score is not None and ep.score.scorecard is not None
    return {
        "seed": seed,
        "synthetic": True,
        "family": rec.case.truth.family.value,
        "trace_head": rec.trace_head,
        "events": list(rec.events),
        "allocation": rec.outcome.allocation.model_dump(mode="json"),
        "scorecard": ep.score.scorecard.summary(),
    }


@pytest.mark.parametrize("seed", GOLDEN_SEEDS)
def test_golden_ach_episodes(seed: int) -> None:
    path = GOLDEN / f"ach_reference_seed{seed}.json"
    got = json.dumps(golden_trace(seed), sort_keys=True, indent=1) + "\n"
    if os.environ.get("SCRUPLE_REGEN_GOLDEN"):
        path.write_text(got)
    assert path.read_text() == got, "regenerate with SCRUPLE_REGEN_GOLDEN=1"
