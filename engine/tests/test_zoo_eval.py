"""Zoo evaluation: bootstrap, paired summaries, the table, and the CLI end to end."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.bench.core import BenchFile, build
from scruple.bench.runner import CaseRow
from scruple.cli.main import main
from scruple.sim.splits import SeedSpace
from scruple.zoo.evaluate import Bootstrap, agents_for, play, render, summarise
from scruple.zoo.protocol import ZooPolicy
from scruple.zoo.rules import ExpertAgent, RandomAgent
from scruple.zoo.train import TrainedAgent

SALT = "zoo-eval-test-salt-0123456789"


def row(i: int, cost: int, *, correct: bool = True, gate: bool = True, **extra: float) -> CaseRow:
    return CaseRow(
        index=i,
        family="true_fraud",
        tier="aligned",
        digest="d",
        reward=1.0 if gate else 0.0,
        quality=1.0,
        gate=gate,
        hard_failures=() if gate else ("EVD-01",),
        failed=(),
        cost=cost,
        baseline_cost=None,
        correct=correct,
        won=cost == 0,
        lapsed=False,
        tool_calls=3,
        amount_minor=1_000,
        extra=dict(extra),
    )


def test_bootstrap_is_seeded_and_brackets_the_mean() -> None:
    xs = [float(i % 7) for i in range(200)]
    a, b = Bootstrap(200, 500, seed=1), Bootstrap(200, 500, seed=1)
    assert a.ci(xs) == b.ci(xs)
    ci = a.ci(xs)
    assert ci["low"] <= ci["mean"] <= ci["high"]
    assert ci["mean"] == pytest.approx(np.mean(xs))
    with pytest.raises(ValueError, match="different number"):
        a.ci(xs[:10])


@given(st.lists(st.floats(min_value=-1e4, max_value=1e4), min_size=2, max_size=60))
def test_bootstrap_interval_is_ordered(xs: list[float]) -> None:
    ci = Bootstrap(len(xs), 200, seed=0).ci(xs)
    assert ci["low"] <= ci["high"]
    assert min(xs) - 1e-6 <= ci["low"]
    assert ci["high"] <= max(xs) + 1e-6


def test_bootstrap_interval_narrows_with_more_cases() -> None:
    rng = np.random.default_rng(0)
    small, big = rng.random(50).tolist(), rng.random(2_000).tolist()
    w = [(c["high"] - c["low"]) for c in (Bootstrap(50).ci(small), Bootstrap(2_000).ci(big))]
    assert w[1] < w[0]


def test_summarise_pairs_against_the_evidence_rows() -> None:
    base = [row(i, 100) for i in range(10)]
    cand = [
        row(i, 0 if i < 4 else 100, correct=i >= 2, uploaded=float(i == 9), exploit=float(i == 9))
        for i in range(10)
    ]
    s = summarise({"evidence": base, "cand": cand}, b=200)
    assert s["evidence"]["vs_evidence"]["cost"]["mean"] == 0.0
    assert s["cand"]["vs_evidence"]["cost"]["mean"] == pytest.approx(-40.0)
    assert s["cand"]["vs_evidence"]["fair"]["mean"] == pytest.approx(-0.2)
    assert s["cand"]["vs_evidence"]["cheaper_cases"] == 4
    assert s["cand"]["vs_evidence"]["dearer_cases"] == 0
    assert s["cand"]["exploit_episodes"] == 1
    assert s["cand"]["exploits"]["uploaded"] == 1
    assert s["cand"]["fair"]["mean"] == pytest.approx(0.8)
    fam = s["cand"]["vs_evidence"]["by_family"]["true_fraud"]
    assert fam == {"n": 10, "cost": -40.0, "fair_pp": -20.0, "win_pp": 40.0}
    assert set(s["cand"]["vs_evidence"]["by_tier"]) == {"aligned"}


def test_summarise_refuses_unpaired_runs() -> None:
    with pytest.raises(ValueError, match="different cases"):
        summarise({"evidence": [row(0, 1), row(1, 1)], "x": [row(1, 1), row(0, 1)]}, b=10)


def test_summarise_counts_hard_failures() -> None:
    base = [row(i, 100) for i in range(4)]
    bad = [row(i, 100, gate=i % 2 == 0) for i in range(4)]
    s = summarise({"evidence": base, "bad": bad}, b=50)
    assert s["bad"]["hard_failures"] == {"EVD-01": 2}
    assert s["bad"]["hard_failure_episodes"] == 2
    assert s["bad"]["gate"]["mean"] == 0.5


def test_render_lists_every_agent() -> None:
    base = [row(i, 1_000) for i in range(5)]
    s = summarise({"evidence": base, "expert_rules": base}, b=20)
    text = render(
        s, {"expert_rules": {"kind": "rule", "objective": "scorecard", "train_episodes": 0}}
    )
    lines = text.splitlines()
    assert len(lines) == 4
    assert "| evidence | reference |" in lines[2]
    assert "| expert_rules | rule | scorecard | 10.00" in lines[3]


# --- on a tiny benchmark ------------------------------------------------------------


@cache
def tiny_bench(tmp: str) -> Path:
    f, _ = build(SeedSpace(SALT), 1, 8, block=4, agents=("evidence",))
    p = Path(tmp) / "manifest.json"
    f.dump(p)
    return p


def _trained(agent: ExpertAgent | RandomAgent, name: str) -> TrainedAgent:
    return TrainedAgent(
        name=name,
        kind=agent.kind,
        objective="scorecard",
        policy=ZooPolicy(agent, name=name),
        config={},
        claim_win=False,
        train_episodes=0,
        train_seconds=0.0,
        dev_episodes=0,
        dev_seconds=0.0,
        dev_value=0.0,
    )


def test_play_scores_every_agent_on_the_same_benchmark_cases(tmp_path: Path) -> None:
    bench = BenchFile.load(tiny_bench(str(tmp_path)))
    trained = {
        "expert_rules": _trained(ExpertAgent(), "expert_rules"),
        "random": _trained(RandomAgent(), "random"),
    }
    agents = agents_for(trained, ("evidence",))
    rows, batch, secs = play(bench, SeedSpace(SALT), agents, limit=None, jobs=1)
    assert set(rows) == {"evidence", "expert_rules", "random"}
    assert all([r.index for r in v] == list(range(8)) for v in rows.values())
    assert all("exploit" in r.extra for v in rows.values() for r in v)
    s = summarise(rows, batch=batch, b=100)
    assert s["evidence"]["vs_evidence"]["cost"]["mean"] == 0.0
    assert set(secs) == set(rows)
    with pytest.raises(PermissionError):
        play(bench, SeedSpace("wrong-salt-0123456789abc"), agents, limit=None, jobs=1)


def test_cli_train_then_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scruple.zoo.train as t

    monkeypatch.setattr(t, "GRIDS", {k: v[:1] for k, v in t.GRIDS.items()})
    models, out = tmp_path / "models.pkl", tmp_path / "out"
    code = main(
        [
            "zoo",
            "train",
            "--train",
            "12",
            "--dev",
            "6",
            "--learners",
            "logreg,q_learning",
            "--rollouts",
            "4",
            "--models",
            str(models),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    summary = json.loads((out / "train.json").read_text())
    assert summary["synthetic"] is True
    assert set(summary["agents"]) == {
        "random",
        "expert_rules",
        "logreg",
        "q_learning",
        "logreg_proxy",
        "q_learning_proxy",
    }
    bench = tiny_bench(str(tmp_path))
    monkeypatch.setenv("SCRUPLE_HELDOUT_SALT", SALT)
    monkeypatch.setattr("scruple.zoo.evaluate.REFERENCES", ("evidence",))
    monkeypatch.setattr("scruple.zoo.cli.REFERENCES", ("evidence",))
    capsys.readouterr()
    code = main(
        [
            "zoo",
            "eval",
            "--bench",
            str(bench),
            "--models",
            str(models),
            "--agents",
            "expert_rules,logreg_proxy",
            "--bootstrap",
            "50",
            "--out",
            str(out),
            "--cache",
            str(tmp_path / "played"),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "synthetic" in printed
    assert "| logreg_proxy | supervised | proxy |" in printed
    result = json.loads((out / "eval.json").read_text())
    assert result["synthetic"] is True
    assert result["benchmark"]["n_cases_scored"] == 8
    assert result["benchmark"]["subset"] == "all cases"
    assert set(result["agents"]) == {"evidence", "expert_rules", "logreg_proxy"}
    assert result["training"]["logreg_proxy"]["claim_win"] is True
    assert SALT not in (out / "eval.json").read_text()


def test_play_cache_reads_back_exactly_and_tracks_the_agent(tmp_path: Path) -> None:
    from scruple.zoo.evaluate import fingerprint

    bench = BenchFile.load(tiny_bench(str(tmp_path)))
    agents = agents_for({"expert_rules": _trained(ExpertAgent(), "expert_rules")}, ("evidence",))
    cache = tmp_path / "played"
    first, b1, _ = play(bench, SeedSpace(SALT), agents, limit=None, jobs=1, cache=cache)
    files = sorted(p.name for p in cache.iterdir())
    assert len(files) == 2
    again, b2, _ = play(bench, SeedSpace(SALT), agents, limit=4, jobs=1, cache=cache)
    assert again["expert_rules"] == first["expert_rules"][:4]  # a prefix of whole blocks
    assert b2.keys() == b1.keys()
    assert sorted(p.name for p in cache.iterdir()) == files  # nothing replayed
    other = agents_for({"expert_rules": _trained(ExpertAgent(escalate_min=1), "expert_rules")}, ())
    assert fingerprint(other["expert_rules"]) != fingerprint(agents["expert_rules"])
    assert fingerprint(agents["evidence"]) == fingerprint(agents_for({}, ("evidence",))["evidence"])


def test_load_models_refuses_other_files(tmp_path: Path) -> None:
    import pickle

    from scruple.zoo.cli import load_models

    p = tmp_path / "x.pkl"
    p.write_bytes(pickle.dumps([1, 2]))
    with pytest.raises(ValueError, match="not a zoo models file"):
        load_models(p)
