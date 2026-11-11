"""`scruple pilot readout`: the template filled from pilot-run outputs, criteria
evaluated pass / fail / not enough data, deterministic markdown and PDF. SYNTHETIC
histories only."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.cli.main import main
from scruple.pilot.readout import build_readout, load_criteria, render_pdf, write_readout
from scruple.pilot.run import MANIFEST, PilotOptions, run_pilot

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "pilot" / "demo"
TEMPLATE = ROOT / "docs" / "pilot" / "READOUT_TEMPLATE.md"
REPORTED = {"fraudulent": 0.5, "general": 0.3, "product_not_received": 0.25}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """main run, an identical rerun, a rerun with other options, a planted bad rule
    and an agent plant (cannot be gated), all on one SYNTHETIC history."""
    base = tmp_path_factory.mktemp("pilot")
    hist = base / "hist.json"
    hist.write_text(json.dumps(stripe_disputes(DEFAULT_PROFILE, 500, 5), sort_keys=True))
    agent = base / "agent.py"
    agent.write_text("def decide(view):\n    return None\n")
    plan = {
        "main": (DEMO / "fraud.json", 200),
        "rerun1": (DEMO / "fraud.json", 200),
        "rerun2": (DEMO / "fraud.json", 200),
        "other": (DEMO / "fraud.json", 300),
        "plant-bad": (DEMO / "bad.json", 200),
        "plant-agent": (agent, 200),
    }
    for name, (cand, n) in plan.items():
        run_pilot(
            PilotOptions(history=str(hist), candidate=str(cand), out=str(base / name), n_cases=n)
        )
    return base


def criteria(base: Path, *items: dict[str, Any], name: str = "criteria.json") -> Path:
    p = base / name
    doc = {"partner": "SYNTHETIC example", "signed": "2026-11-11", "criteria": list(items)}
    p.write_text(json.dumps(doc))
    return p


FULL = (
    {"id": "H1", "metric": "planted_blocked", "plants": [{"id": "p1", "run": "plant-bad"}]},
    {"id": "H2", "metric": "new_hard_violations", "max": 0},
    {"id": "H3", "metric": "self_report_gap", "min_cases": 30, "reported": REPORTED},
    {"id": "H4", "metric": "identical_reruns", "reruns": ["rerun1", "rerun2"], "required": 2},
    {"id": "H5", "metric": "improvement_every_reason"},
)


def verdicts(base: Path, *items: dict[str, Any]) -> dict[str, str]:
    r = build_readout(base / "main", criteria(base, *items, name="c-" + items[0]["id"] + ".json"))
    return {x.id: x.verdict for x in r.results}


def test_full_criteria_on_a_synthetic_run(runs: Path) -> None:
    r = build_readout(runs / "main", criteria(runs, *FULL))
    got = {x.id: x.verdict for x in r.results}
    assert got["H1"] == "pass"  # the bad rule is blocked by the gate
    assert got["H2"] == "pass"
    assert got["H4"] == "pass"
    assert got["H5"] == "not enough data"  # fraud is short of its planned sample size
    # product_unacceptable has 46 contested cases (>= 30) but no reported figure
    assert got["H3"] == "fail"
    assert r.data == "synthetic" and "(SYNTHETIC DATA)" in r.markdown.splitlines()[0]
    assert [x.id for x in r.results] == ["H1", "H2", "H3", "H4", "H5"]


def test_every_template_section_is_filled_in_order(runs: Path) -> None:
    md = build_readout(runs / "main", criteria(runs, *FULL)).markdown
    want = [
        re.sub(r" \(exists\)$", "", h) for h in re.findall(r"^## .*$", TEMPLATE.read_text(), re.M)
    ]
    got = re.findall(r"^## .*$", md, re.M)
    assert [g.split(" (")[0] for g in got] == [w.split(" (")[0] for w in want]
    assert "{{" not in md  # every computed placeholder is filled


def test_numbers_trace_to_the_run_files(runs: Path) -> None:
    md = build_readout(runs / "main", criteria(runs, *FULL)).markdown
    ing = json.loads((runs / "main" / "01-ingest" / "ingest.json").read_text())
    card = json.loads((runs / "main" / "05-gate" / "scorecard.json").read_text())
    assert ing["input_sha256"] in md and card["audit_head"] in md
    assert f"| records read | {ing['rows_read']} |" in md


def test_output_is_deterministic(runs: Path, tmp_path: Path) -> None:
    c = criteria(runs, *FULL)
    a = write_readout(build_readout(runs / "main", c), tmp_path / "a")
    b = write_readout(build_readout(runs / "rerun1", c), tmp_path / "b")
    for x, y in zip(a, b, strict=True):
        assert x.read_bytes() == y.read_bytes(), x.name
    r = build_readout(runs / "main", c)
    assert render_pdf(r) == render_pdf(r) and render_pdf(r)[:5] == b"%PDF-"


def test_self_report_gap_rules(runs: Path) -> None:
    gap = {"id": "H3", "metric": "self_report_gap", "min_cases": 30}
    assert verdicts(runs, gap)["H3"] == "not enough data"  # nothing reported
    assert verdicts(runs, {**gap, "reported": {"fraudulent": 0.5}})["H3"] == "fail"
    wins = json.loads((runs / "main" / "02-calibration" / "calibration.json").read_text())
    every = {k: 0.4 for k in wins["aggregates"]["win_by_reason"]}
    assert verdicts(runs, {**gap, "reported": every})["H3"] == "pass"
    assert verdicts(runs, {**gap, "reported": every, "min_cases": 10**6})["H3"] == (
        "not enough data"
    )


def test_rerun_rules(runs: Path) -> None:
    h4 = {"id": "H4", "metric": "identical_reruns", "required": 2}
    assert verdicts(runs, {**h4, "reruns": ["rerun1"]})["H4"] == "not enough data"
    assert verdicts(runs, {**h4, "reruns": ["rerun1", "other"]})["H4"] == "fail"


def test_plant_rules(runs: Path) -> None:
    h1 = {"id": "H1", "metric": "planted_blocked"}
    assert verdicts(runs, {**h1, "plants": [{"id": "a", "run": "plant-agent"}]})["H1"] == "fail"
    wrong_code = [{"id": "b", "run": "plant-bad", "reason": "fraudulent"}]
    assert verdicts(runs, {**h1, "plants": wrong_code})["H1"] == "fail"


def test_improvement_rules(runs: Path) -> None:
    h5 = {"id": "H5", "metric": "improvement_every_reason"}
    assert verdicts(runs, {**h5, "reasons": ["general"]})["H5"] == "fail"  # no change
    assert verdicts(runs, {**h5, "reasons": ["missing"]})["H5"] == "fail"


def test_violation_rule(runs: Path, tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "main"
    shutil.copytree(runs / "main", copy)
    m = json.loads((copy / MANIFEST).read_text())
    for s in m["steps"]:
        if s["name"] == "shadow":
            s["summary"]["candidate_new_hard_findings"] = 3
    (copy / MANIFEST).write_text(json.dumps(m))
    c = criteria(tmp_path, {"id": "H2", "metric": "new_hard_violations", "max": 2})
    assert build_readout(copy, c).results[0].verdict == "fail"


def test_criteria_in_toml(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('partner = "x"\n[[criteria]]\nid = "H2"\nmetric = "new_hard_violations"\n')
    assert load_criteria(p).criteria[0].id == "H2"


def test_cli(runs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = criteria(runs, *FULL)
    rc = main(["pilot", "readout", "--run", str(runs / "main"), "--criteria", str(c), "--out",
               str(tmp_path / "r")])  # fmt: skip
    assert rc == 0
    out = capsys.readouterr().out
    assert "[SYNTHETIC DATA]" in out and "not enough data H5" in out
    assert {p.name for p in (tmp_path / "r").iterdir()} == {
        "readout.md",
        "readout.pdf",
        "readout.json",
    }
    bad = criteria(tmp_path, {"id": "H9", "metric": "vibes"})
    assert main(["pilot", "readout", "--run", str(runs / "main"), "--criteria", str(bad),
                 "--out", str(tmp_path / "x")]) == 2  # fmt: skip
