"""The committed AI-types results: labelled synthetic, and the doc matches the JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "results" / "zoo" / "eval.json"
TRAIN = ROOT / "results" / "zoo" / "train.json"
DOC = ROOT / "docs" / "results" / "AI_TYPES.md"

pytestmark = pytest.mark.skipif(not EVAL.exists(), reason="no committed zoo results")


def _eval() -> dict[str, Any]:
    out: dict[str, Any] = json.loads(EVAL.read_text())
    return out


def _ci(e: dict[str, float], scale: float, fmt: str) -> str:
    return (
        f"{fmt.format(e['mean'] * scale)} "
        f"[{fmt.format(e['low'] * scale)}, {fmt.format(e['high'] * scale)}]"
    )


def _rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    text = DOC.read_text()
    for line in text[text.index("## Results") :].splitlines():
        if line.startswith("| ") and not line.startswith("| agent"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            name = cells[0].removeprefix("ref: ").split(" (")[0]
            rows.setdefault(name, cells)
    return rows


def test_results_are_labelled_synthetic_and_declare_the_subset() -> None:
    res = _eval()
    assert res["synthetic"] is True
    assert "Synthetic" in res["note"]
    b = res["benchmark"]
    assert b["n_cases_scored"] <= b["n_cases_total"]
    assert ("the same for every agent" in b["subset"]) or b["subset"] == "all cases"
    train = json.loads(TRAIN.read_text())
    assert train["synthetic"] is True
    assert train["config"]["train_split"] == "train"
    assert train["config"]["dev_split"] == "dev"
    assert DOC.read_text().startswith("# AI types")
    assert "Synthetic." in DOC.read_text()


def test_every_agent_was_scored_on_the_same_cases() -> None:
    res = _eval()
    ns = {a["n_cases"] for a in res["agents"].values()}
    assert ns == {res["benchmark"]["n_cases_scored"]}
    assert res["agents"]["evidence"]["vs_evidence"]["cost"]["mean"] == 0.0


def test_doc_tables_match_the_json() -> None:
    res = _eval()["agents"]
    rows = _rows()
    assert set(res) <= set(rows), set(res) - set(rows)
    for name, s in res.items():
        cells = rows[name]
        assert cells[1] == _ci(s["cost"], 0.01, "{:.2f}"), name
        if name != "evidence":
            assert cells[2] == _ci(s["vs_evidence"]["cost"], 0.01, "{:+.2f}"), name
        assert cells[3] == _ci(s["gate"], 100, "{:.1f}"), name
        assert cells[4] == _ci(s["fair"], 100, "{:.1f}"), name
        assert cells[6] == f"{100 * s['win']['mean']:.1f}", name


def test_doc_hard_failure_counts_match_the_json() -> None:
    res = _eval()["agents"]
    rows = _rows()
    for name in ("logreg_proxy", "bandit_linucb_proxy", "q_learning_proxy", "mc_planner_proxy"):
        cells = rows[name]
        assert cells[7] == str(res[name]["exploits"]["uploaded"])
        assert cells[8] == str(res[name]["exploits"]["false_claim"])
        for check, n in res[name]["hard_failures"].items():
            assert f"{check} {n}" in cells[9], (name, check)


def test_scorecard_agents_never_exploit_and_proxy_twins_do() -> None:
    res = _eval()
    train = res["training"]
    for name, s in res["agents"].items():
        t = train.get(name, {})
        if t.get("objective") == "scorecard":
            assert s["exploit_episodes"] == 0, name
            assert t["claim_win"] is False
        if t.get("objective") == "proxy":
            assert t["claim_win"] is True
            assert s["exploits"]["false_claim"] > 0
