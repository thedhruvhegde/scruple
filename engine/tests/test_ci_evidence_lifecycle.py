"""Evidence packs state the version's real history: the gate verdict when it was made,
the current status and the rollback, never "BLOCK" for a version that passed and was
later rolled back by an operator (synthetic data)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader

from scruple.ci.evidence import build_evidence_pack, build_lifecycle, load_pack
from scruple.ci.evidence_pdf import render_pdf
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.registry.models import RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import APPROVER, PROV

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)


def _active(path: str = ":memory:") -> AgentGate:
    g = AgentGate(SQLiteStorage(path), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.approve(GOOD_RULE_ID, 1, APPROVER)
    return g


@pytest.fixture(scope="module")
def rolled_back() -> AgentGate:
    g = _active()
    g.rollback(GOOD_RULE_ID, APPROVER, "pilot rollback drill")
    return g


def _text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def test_rolled_back_pack_keeps_the_gate_verdict(rolled_back: AgentGate) -> None:
    pack = build_evidence_pack(rolled_back, GOOD_RULE_ID, 1)
    lc = pack.lifecycle
    assert pack.status == "retired" and lc.current_status == "retired"
    assert lc.gate_verdict == "pass" and lc.gate_stage == "activation"
    assert pack.scorecard.verdict.value == "pass"
    assert lc.rolled_back is not None
    assert lc.rolled_back.actor == APPROVER and "pilot rollback drill" in lc.rolled_back.reason
    assert lc.rolled_back.decision_id == pack.decisions[-1].decision_id
    assert lc.rolled_back.restored_version is None
    assert pack.decisions[-1].stage.value == "rollback"  # the record is still there


def test_lifecycle_events_are_in_chain_order_and_complete(rolled_back: AgentGate) -> None:
    lc = build_evidence_pack(rolled_back, GOOD_RULE_ID, 1).lifecycle
    seqs = [e.seq for e in lc.events]
    assert seqs == sorted(seqs)
    kinds = [e.event for e in lc.events]
    assert kinds[0] == "rule.registered" and kinds[-1] == "rule.rolled_back"
    assert "rule.activated" in kinds
    verdicts = [(e.stage, e.verdict_at_decision) for e in lc.events if e.decision_id]
    assert verdicts == [
        ("shadow", "pass"),
        ("canary", "pass"),
        ("activation", "pass"),
        ("rollback", None),
    ]
    statuses = [e.to_status for e in lc.events if e.decision_id is None]
    assert statuses[-1] == "retired" and "active" in statuses
    assert "Gate verdict PASS" in lc.statement and "Rolled back by" in lc.statement
    assert lc.statement.endswith("Current status: retired.")


def test_pdf_after_rollback_states_the_history(rolled_back: AgentGate) -> None:
    text = _text(render_pdf(build_evidence_pack(rolled_back, GOOD_RULE_ID, 1)))
    assert "Gate verdict: PASS" in text
    assert "Current status: retired" in text
    assert "Rolled back: by model-risk@demo.bank" in text and "pilot rollback drill" in text
    assert "Verdict: BLOCK" not in text and "BLOCK" not in text.split("History")[0]
    assert "None ->" not in text and "-> candidate" in text


def test_pack_before_and_after_rollback(tmp_path: Path) -> None:
    g = _active()
    before = build_evidence_pack(g, GOOD_RULE_ID, 1)
    assert before.lifecycle.rolled_back is None and before.lifecycle.current_status == "active"
    g.rollback(GOOD_RULE_ID, APPROVER, "drill")
    after = build_evidence_pack(g, GOOD_RULE_ID, 1)
    assert before.lifecycle.gate_verdict == after.lifecycle.gate_verdict == "pass"
    assert before.scorecard.verdict == after.scorecard.verdict
    assert len(after.lifecycle.events) > len(before.lifecycle.events)


def test_retired_by_a_newer_version() -> None:
    g = _active()
    spec = GOOD_RULE.model_copy(update={"description": "v2 of the rule", "priority": 60})
    g.propose(GOOD_RULE_ID, spec, "ops-team", PROV)
    # v2 is moved through the registry directly (its own gate run is not the point).
    for to in (RuleStatus.SHADOW, RuleStatus.CANARY):
        g.registry.transition(GOOD_RULE_ID, 2, to, APPROVER, "test")
    g.registry.activate(GOOD_RULE_ID, 2, APPROVER, "test")
    lc = build_evidence_pack(g, GOOD_RULE_ID, 1).lifecycle
    assert lc.current_status == "retired" and lc.retired_by_version == 2
    assert lc.rolled_back is None and lc.gate_verdict == "pass"
    assert "Retired when v2 was activated." in lc.statement
    # Rolling v2 back restores v1, and v2's pack says so.
    g.rollback(GOOD_RULE_ID, APPROVER, "v2 misbehaved")
    lc2 = build_evidence_pack(g, GOOD_RULE_ID, 2).lifecycle
    assert lc2.rolled_back is not None and lc2.rolled_back.restored_version == 1
    assert build_evidence_pack(g, GOOD_RULE_ID, 1).lifecycle.current_status == "active"


def test_lifecycle_is_pure_and_pack_round_trips(rolled_back: AgentGate) -> None:
    records = [r for r in rolled_back.decisions(GOOD_RULE_ID) if r.decision.version == 1]
    entries = rolled_back.audit.entries()
    a = build_lifecycle(records, entries, GOOD_RULE_ID, 1, "retired")
    b = build_lifecycle(records, entries, GOOD_RULE_ID, 1, "retired")
    assert a == b
    pack = build_evidence_pack(rolled_back, GOOD_RULE_ID, 1)
    loaded, claimed = load_pack(pack.to_json())
    assert loaded == pack and claimed == pack.pack_sha256
    assert pack.schema_version == 2


def test_cli_evidence_after_rollback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "g.db"
    g = _active(str(db))
    g.rollback(GOOD_RULE_ID, APPROVER, "drill")
    g.storage.close()
    out = tmp_path / "ev"
    assert main(["ci", "evidence", "--db", str(db), "--out", str(out), GOOD_RULE_ID, "1"]) == 0
    text = _text((out / f"evidence-{GOOD_RULE_ID}-v1.pdf").read_bytes())
    assert "Gate verdict: PASS" in text and "Current status: retired" in text
