"""The 40 hand-written scenarios (035): sourced, varied, and correct through the engine."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scruple.cli.main import main
from scruple.sim.families import CLAIM_VALID, CaseFamily
from scruple.sim.models import Stage
from scruple.sim.networks import Network, Workflow
from scruple.sim.scenario import (
    SCENARIO_DIR,
    Expected,
    Scenario,
    load_scenarios,
    run_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = load_scenarios()
BY_ID = {s.id: s for s in SCENARIOS}


SOURCE_IDS = set(
    re.findall(r"(?m)^\| (S\d+) \|", (ROOT / "docs/SOURCES.md").read_text(encoding="utf-8"))
)
ASSUMPTION_IDS = set(
    re.findall(r"\*\*(A-\d+)\*\*", (ROOT / "docs/ASSUMPTIONS.md").read_text(encoding="utf-8"))
)


def test_there_are_forty_with_unique_ids_matching_file_names() -> None:
    files = sorted(SCENARIO_DIR.glob("*.yaml"))
    assert len(files) == 40 == len(SCENARIOS) == len(BY_ID)
    assert {f.stem for f in files} == set(BY_ID)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_is_correct_through_the_engine(sc: Scenario) -> None:
    run = run_scenario(sc)
    assert run.passed, run.problems
    assert run.outcome is not None and run.outcome.correct
    assert all(r.ok for r in run.results)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_cites_known_sources(sc: Scenario) -> None:
    assert SOURCE_IDS and ASSUMPTION_IDS
    for ref in sc.sources:
        known = SOURCE_IDS if ref.startswith("S") else ASSUMPTION_IDS
        assert ref in known, f"{sc.id} cites unknown {ref}"
    assert len(sc.expected.why) >= 40


@pytest.mark.parametrize(
    "sc",
    [s for s in SCENARIOS if not CLAIM_VALID[s.case.family]],
    ids=lambda s: s.id,
)
def test_accepting_an_invalid_claim_is_wrong(sc: Scenario) -> None:
    """The naive 'always accept' answer pays a cardholder who is not owed (or, at an
    inquiry, cannot be given at all)."""
    stage = sc.case.opening_stage
    if stage is Stage.INQUIRY:
        wrong = sc.model_copy(
            update={"history": (), "expected": _expected("refund", sc.case.amount_minor)}
        )
    else:
        wrong = sc.model_copy(update={"history": (), "expected": _expected("accept")})
    run = run_scenario(wrong)
    assert run.outcome is not None and not run.outcome.correct


@pytest.mark.parametrize(
    "sc",
    [s for s in SCENARIOS if s.expected.action == "accept" and not s.history],
    ids=lambda s: s.id,
)
def test_contesting_a_valid_claim_costs_more(sc: Scenario) -> None:
    """Representing with everything held instead of accepting only adds fees."""
    case = sc.build()
    right = run_scenario(sc)
    ids = tuple(k for k in sorted(sc.evidence_refs()) if k not in ("refund_record",))
    alt = sc.model_copy(
        update={
            "expected": Expected(
                day=sc.expected.day,
                action="represent",
                evidence=tuple(i for i in ids if _unique_ref(sc, i)),
                why="contest the claim with every document held",
            ),
            "checks": sc.checks.model_copy(update={"incidents": None}),
        }
    )
    wrong = run_scenario(alt)
    assert right.outcome is not None and wrong.outcome is not None
    assert wrong.outcome.fees_usd_minor > right.outcome.fees_usd_minor, case.case_id


def _unique_ref(sc: Scenario, ref: str) -> bool:
    docs, labels, _ = sc.documents()
    return ref in labels or sum(1 for d in docs if d.kind.value == ref) == 1


def _expected(action: str, amount: int | None = None) -> Expected:
    return Expected.model_validate(
        {"action": action, "amount_minor": amount, "why": "the naive alternative answer"}
    )


def test_the_set_covers_the_rule_space() -> None:
    cases = [s.build() for s in SCENARIOS]
    assert {c.network for c in cases} == set(Network)
    assert {c.opening_stage for c in cases} == {Stage.INQUIRY, Stage.CHARGEBACK}
    assert {c.truth.family for c in cases} == set(CaseFamily)
    assert {c.workflow for c in cases} == set(Workflow)
    assert {s.expected.action for s in SCENARIOS} == {
        "accept",
        "represent",
        "escalate",
        "refund",
        "wait",
    }
    assert {c.currency for c in cases} >= {"USD", "EUR", "JPY"}
    assert any(c.shipments for c in cases) and any(c.refund_in_flight for c in cases)
    assert sum(1 for c in cases if c.prior_transactions) >= 7
    assert any(c.truth.forged for c in cases) and any(c.truth.missing_evidence for c in cases)
    cited = {r for s in SCENARIOS for r in s.sources}
    assert len(cited & SOURCE_IDS) >= 8 and len(cited & ASSUMPTION_IDS) >= 8


def test_ce3_checks_match_the_rule() -> None:
    with_check = [s for s in SCENARIOS if s.checks.ce3_eligible is not None]
    assert len(with_check) >= 8
    assert {s.checks.ce3_eligible for s in with_check} == {True, False}


def test_cli_runs_every_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sim", "scenarios"]) == 0
    out = capsys.readouterr().out
    assert "40/40 scenarios passed" in out


def test_cli_reports_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = (SCENARIO_DIR / "visa-10-4-true-fraud-accept.yaml").read_text()
    bad = text.replace("  action: accept", "  action: wait")
    (tmp_path / "bad.yaml").write_text(bad)
    assert main(["sim", "scenarios", "--dir", str(tmp_path)]) == 1
    assert "FAIL visa-10-4-true-fraud-accept" in capsys.readouterr().out
