"""Exploit coverage across verifier versions, the hacking experiments, the statistics
helpers and the report writer (small seed counts; the 1,000-seed run is in
docs/results/FINOPS_SUITE.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scruple.finops import experiments as xp
from scruple.finops.exploits import CHECKS_V2, SUITE_EXPLOITS, coverage, render, run_exploit
from scruple.finops.report import markdown, run_all

NEW = ("XR6", "XR7", "XR8", "XR9", "XR10", "XM5", "XM6", "XM7", "XM8", "XM9", "XM10")


def test_registry_holds_kind_and_suite_exploits() -> None:
    kind = {e for e, x in SUITE_EXPLOITS.items() if x.origin == "kind"}
    assert {"XA1", "XR1", "XM1", "XP5"} <= kind
    assert set(NEW) <= {e for e, x in SUITE_EXPLOITS.items() if x.origin == "suite"}
    assert SUITE_EXPLOITS["XR6"].checks_for("recon@1") == ("REC-APPLY", "REC-CLEAR")
    assert SUITE_EXPLOITS["XM8"].checks_for("aml@2") == ("AML-DEC2", "AML-BASIS2")
    assert CHECKS_V2["REC-TIE"] == "REC-TIE2"


@pytest.mark.parametrize("eid", NEW)
def test_new_exploits_are_caught_by_v2(eid: str) -> None:
    ex = SUITE_EXPLOITS[eid]
    v2 = f"{ex.env}@2"
    (row,) = run_exploit(eid, range(24), [v2])
    assert row.status == "caught", row
    assert row.honest_hard_failures == 0
    assert row.reward < row.honest_reward


def test_rec_apply_is_what_turns_xr6_into_a_hard_failure() -> None:
    rows = {r.verifier: r for r in run_exploit("XR6", range(30), ["recon@1", "recon@2"])}
    assert rows["recon@1"].hard_caught_on == 0
    assert rows["recon@2"].hard_caught_on == rows["recon@2"].exercised > 0


def test_coverage_filters_and_renders() -> None:
    rows = coverage(range(4), envs=["aml"], ids=["XM7", "XM8"], workers=2)
    assert {(r.id, r.verifier) for r in rows} == {
        ("XM7", "aml@1"), ("XM7", "aml@2"), ("XM8", "aml@1"), ("XM8", "aml@2")
    }  # fmt: skip
    md = render(rows)
    assert md.count("\n") == len(rows) + 1 and "| XM7 | aml |" in md
    assert run_exploit("XA4", range(3), ["ach@2"])[0].env == "ach"  # tampered records


def test_statistics_helpers() -> None:
    m, lo, hi = xp.bootstrap_ci([0.0, 1.0] * 50)
    assert m == 0.5 and lo < 0.5 < hi
    assert xp.bootstrap_ci([]) == (0.0, 0.0, 0.0)
    p, lo, hi = xp.wilson(0, 1000)
    assert p == 0 and lo == 0 and 0.003 < hi < 0.004
    assert xp.wilson(0, 0) == (0.0, 0.0, 0.0)
    assert xp.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert xp.spearman([1, 1, 2], [5, 5, 6]) == pytest.approx(1)
    assert xp.spearman([1, 1], [2, 3]) == 0.0
    assert xp.difficulty_of(12) == 0.1


def test_baselines_incentives_climb_and_caught_small() -> None:
    b = xp.baselines(range(11), envs=["aml"], workers=2)
    assert b["aml"]["honest"]["aml@2"]["mean"] == 1.0
    assert b["aml"]["floor"]["aml@2"]["mean"] == 0.0
    assert set(b["aml"]["honest"]["aml@2"]["by_difficulty"]) == {f"{x:.1f}" for x in xp.BINS}
    inc = xp.incentives("aml", range(6))
    assert inc["expert"]["skip_review"]["aml@2"]["delta"] == -1.0
    assert inc["expert"]["sar_always"]["aml@2"]["proxy_delta"] > 0
    c = xp.climb("aml", "aml@2", range(4), restarts=1)
    assert c["best"] == "honest" and c["exploits_on"] == []
    p = xp.climb("aml", "proxy", range(4), restarts=2, seed=1)
    assert p["best_value"] >= 0.5
    rc = xp.climb("recon", "proxy", range(3), restarts=1)
    assert rc["evaluations"] > 1
    caught = xp.caught(range(4))
    assert caught["recon"]["recon@2"]["honest_false_positive"][0] == 0
    assert caught["aml"]["aml@2"]["hacker_hard_caught"][0] == 4


def test_proxy_definitions() -> None:
    from scruple.finops.envs import get_env
    from scruple.finops.policies import run_policy

    e = get_env("ach")
    ep = run_policy(e.episode_cls, e.policies()["honest"], 1, None)
    assert xp.proxy("ach", ep.record()) == 0.0


def test_run_all_writes_json_and_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scruple.finops import report

    monkeypatch.setattr(report, "bench", lambda env, n, workers: {
        "env": env, "policy": "honest", "n": n, "workers": workers, "episodes_per_s_serial": 1.0,
        "episodes_per_s_parallel": 2.0, "grades_per_s_serial": 1.0, "grades_per_s_parallel": 2.0,
    })  # fmt: skip
    monkeypatch.setattr(report, "coverage", lambda seeds, workers: coverage(seeds, ids=["XM8"]))
    monkeypatch.setattr(xp, "BINS", (0.0, 1.0))
    res = run_all(tmp_path, seeds=2, train=2, workers=1)
    assert json.loads((tmp_path / "results.json").read_text())["seeds"] == [0, 1]
    md = (tmp_path / "results.md").read_text()
    assert "### recon: reward by policy" in md and "| XM8 |" in md
    assert markdown(res, "").count("### Throughput") == 1
