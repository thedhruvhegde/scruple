"""Committed real-data baseline results: shape, provenance, and that the doc quotes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scruple.realdata.splits import load_manifest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "real"
DOC = ROOT / "docs" / "results" / "REAL_BASELINES.md"
TASKS = ["outcome-2015", "outcome-2025", "regime-2015", "routing-2015", "routing-2025"]


def _load(task: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{task}.json").read_text())
    return data


def test_one_file_per_task() -> None:
    assert sorted(f.stem for f in RESULTS.glob("*.json")) == TASKS


@pytest.mark.parametrize("task", TASKS)
def test_result_file_shape_and_provenance(task: str) -> None:
    res = _load(task)
    assert res["task"] == task
    assert "public domain" in res["data"] and "Not synthetic" in res["data"]
    assert res["reproduce"].startswith("uv run scruple realdata baselines")
    manifest = load_manifest()["datasets"][res["dataset"]]["splits"]
    for s in ("train", "dev", "test"):
        assert res["splits"][s]["sha256_jsonl"] == manifest[s]["sha256_jsonl"]
    assert set(res["results"]) == {"majority", "keywords", "tfidf_lr"}
    for card in res["results"].values():
        assert card["n"] == res["splits"]["test"]["n"]
        assert card["n_invalid"] == 0
        assert card["bootstrap"] == {"n_boot": 1000, "seed": 0, "ci": 0.95}
    assert "timestamp" not in json.dumps(res)


def _fmt(est: dict[str, Any]) -> str:
    return f"{est['value']:.3f} [{est['lo']:.3f}, {est['hi']:.3f}]"


def test_doc_quotes_the_committed_numbers() -> None:
    doc = DOC.read_text()
    for task in ("routing-2015", "routing-2025"):
        for card in _load(task)["results"].values():
            assert _fmt(card["issue"]["accuracy"]) in doc
            assert _fmt(card["issue"]["macro_f1"]) in doc
    for card in _load("routing-2025")["results"].values():
        assert _fmt(card["issue_sub_issue"]["accuracy"]) in doc
    for task in ("outcome-2015", "outcome-2025"):
        for card in _load(task)["results"].values():
            for m in ("auroc", "brier", "ece"):
                assert _fmt(card[m]) in doc, (task, m)
    for card in _load("regime-2015")["results"].values():
        assert _fmt(card["accuracy"]) in doc
        assert _fmt(card["macro_f1"]) in doc
