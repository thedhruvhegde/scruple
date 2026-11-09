"""E-value statistics in the evidence report, the CI scorecard, the PR comment and
the evidence pack. Bootstrap outputs stay byte-identical (no new keys). Synthetic."""

from __future__ import annotations

import json

import pytest

from scruple.ci.comment import render_comment
from scruple.ci.evidence import build_evidence_pack
from scruple.ci.scorecard import build_scorecard, load_scorecard
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.gate.evalue import summary_lines
from scruple.gate.models import DecisionRecord, Outcome
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

# Large MDR so that 8,000 held-out cases satisfy the plan and certify the good rule.
EV = GateConfig(stats="evalue", n_cases=8_000, evalue_min_detectable_regression_fraction=0.1)


def _run(cfg: GateConfig, rule_id: str) -> tuple[AgentGate, list[DecisionRecord]]:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    spec = GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE
    g.propose(rule_id, spec, owner="ops", provenance=PROV)
    return g, g.evaluate(rule_id, 1)


@pytest.fixture(scope="module")
def good() -> tuple[AgentGate, list[DecisionRecord]]:
    return _run(EV, GOOD_RULE_ID)


@pytest.fixture(scope="module")
def bad() -> tuple[AgentGate, list[DecisionRecord]]:
    return _run(EV.model_copy(update={"n_cases": 3_000}), BAD_RULE_ID)


def test_good_rule_reaches_canary_with_sequential_evidence(
    good: tuple[AgentGate, list[DecisionRecord]],
) -> None:
    _, recs = good
    assert [r.decision.outcome for r in recs] == [Outcome.PROMOTE, Outcome.CANARY_PASSED]
    assert recs[0].decision.shadow is not None and recs[0].decision.shadow.evalue is not None
    assert recs[1].decision.canary is not None and recs[1].decision.canary.sequential is not None


def test_scorecard_carries_evalue_and_round_trips(
    good: tuple[AgentGate, list[DecisionRecord]],
) -> None:
    g, recs = good
    card = build_scorecard(recs, g.config, g.audit.head())
    assert card.evalue is not None and card.canary_sequential is not None
    assert card.statistics is not None and "e-BH" in card.statistics.method
    body = json.loads(card.to_json())
    t = body["evalue"]["types"][0]
    for key in ("regression_e_value", "ci_low", "ci_high", "required_n", "alpha_share"):
        assert key in t
    assert body["canary_sequential"]["decision"] in ("harm", "benefit", "no_decision")
    assert load_scorecard(card.to_json()) == card


def test_scorecard_reports_regressed_case_types(
    bad: tuple[AgentGate, list[DecisionRecord]],
) -> None:
    g, recs = bad
    card = build_scorecard(recs, g.config, g.audit.head())
    kinds = {r.kind for r in card.regressions}
    assert "slice" not in kinds  # the zero-tolerance slice rule is not used on this path
    ev = card.evalue
    assert ev is not None and not ev.sufficient


def test_bootstrap_scorecard_has_no_new_keys() -> None:
    g, recs = _run(GateConfig(n_cases=1_000, bootstrap_resamples=200), GOOD_RULE_ID)
    card = build_scorecard(recs, g.config, g.audit.head())
    body = json.loads(card.to_json())
    assert "evalue" not in body and "canary_sequential" not in body
    assert "E-value statistics" not in render_comment(card)


def test_comment_shows_evalue_table(good: tuple[AgentGate, list[DecisionRecord]]) -> None:
    g, recs = good
    text = render_comment(build_scorecard(recs, g.config, g.audit.head()))
    assert "### E-value statistics" in text
    assert "| Case type | Cases | Needs |" in text
    assert "`not_received/<$10,000`" in text
    assert "Sequential two-sample test" in text


def test_comment_flags_insufficient_evidence(
    bad: tuple[AgentGate, list[DecisionRecord]],
) -> None:
    g, recs = bad
    assert "**insufficient evidence**" in render_comment(
        build_scorecard(recs, g.config, g.audit.head())
    )


def test_report_has_evalue_section(good: tuple[AgentGate, list[DecisionRecord]]) -> None:
    g, recs = good
    shadow = g.report(recs[0].decision.decision_id) or ""
    assert "### Anytime-valid statistics (e-values)" in shadow
    assert "confidence sequence" in shadow and "Alpha share" in shadow
    canary = g.report(recs[1].decision.decision_id) or ""
    assert "Sequential two-sample test" in canary and "matched groups" in canary


def test_evidence_pack_statistics(good: tuple[AgentGate, list[DecisionRecord]]) -> None:
    g, _ = good
    pack = build_evidence_pack(g, GOOD_RULE_ID, 1)
    ev = pack.statistics["evalue"]
    assert ev["required_total_cases"] > 0 and ev["types"]
    assert all(t["alpha_share"] > 0 for t in ev["types"])
    assert "sequential" in pack.statistics["canary_stages"][0]
    assert pack.pack_sha256 == build_evidence_pack(g, GOOD_RULE_ID, 1).pack_sha256


def test_summary_lines(good: tuple[AgentGate, list[DecisionRecord]]) -> None:
    _, recs = good
    s = recs[0].decision.shadow
    assert s is not None and s.evalue is not None
    lines = summary_lines(s.evalue)
    assert len(lines) == 1 + len(s.evalue.types)
    assert "plan needs" in lines[0] and "alpha share" in lines[1]


def test_evidence_pdf_renders_with_evalue(good: tuple[AgentGate, list[DecisionRecord]]) -> None:
    pdf = pytest.importorskip("scruple.ci.evidence_pdf")
    g, _ = good
    out = pdf.render_pdf(build_evidence_pack(g, GOOD_RULE_ID, 1))
    assert out.startswith(b"%PDF")
