"""Evidence pack export: deterministic JSON with a chain proof, and a parseable PDF."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from scruple.ci.evidence import build_evidence_pack, load_pack, verify_segment
from scruple.ci.evidence_pdf import render_pdf
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.canary import RampSchedule
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import APPROVER, GOOD, PROV, write_repo

SECRET_SEED = 424_243
CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300, eval_seed=SECRET_SEED)
POLICY = ApprovalPolicy(
    approvers=(
        NamedApprover(name="alice@demo.bank", roles=("model-risk",)),
        NamedApprover(name="bob@demo.bank", roles=("ops-lead",)),
    ),
    required=2,
    required_role="model-risk",
)


def _approved_gate(path: str = ":memory:") -> AgentGate:
    g = AgentGate(SQLiteStorage(path), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.start_canary_ramp(GOOD_RULE_ID, 1, RampSchedule(stages=(10.0, 25.0)))
    for i in range(2):
        g.advance_canary(GOOD_RULE_ID, 1, generate_cases(40 + i, 2_000, prefix=f"e{i}-"))
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, "proposer@demo.bank")
    g.grant_approval(GOOD_RULE_ID, 1, "bob@demo.bank")
    g.grant_approval(GOOD_RULE_ID, 1, "alice@demo.bank")
    return g


@pytest.fixture(scope="module")
def gate() -> AgentGate:
    return _approved_gate()


def test_pack_contents(gate: AgentGate) -> None:
    pack = build_evidence_pack(gate, GOOD_RULE_ID, 1)
    assert pack.data == "synthetic" and pack.status == "active"
    assert pack.scorecard.verdict.value == "pass"
    stages = [d.stage.value for d in pack.decisions]
    assert stages == ["shadow", "canary", "canary", "canary", "activation"]
    assert pack.canary_ramp is not None and pack.canary_ramp.status == "completed"
    assert pack.approvals.policy == POLICY
    assert pack.approvals.satisfied[0]["approvers"] == ["alice@demo.bank", "bob@demo.bank"]
    assert len(pack.statistics["canary_stages"]) == 3
    assert pack.audit.proof.ok and pack.audit.proof.recomputed_head == gate.audit.head()
    assert pack.audit.entries[0].event_type == "rule.registered"
    assert pack.audit.entries[-1].event_type == "rule.activated"


def test_pack_never_contains_operator_held_seeds(gate: AgentGate) -> None:
    text = build_evidence_pack(gate, GOOD_RULE_ID, 1).to_json()
    assert str(SECRET_SEED) not in text
    assert '"eval_seed"' not in text and '"canary_seed"' not in text
    assert "seed_commitments" in text


def test_json_is_deterministic(gate: AgentGate) -> None:
    a = build_evidence_pack(gate, GOOD_RULE_ID, 1).to_json()
    b = build_evidence_pack(gate, GOOD_RULE_ID, 1).to_json()
    c = build_evidence_pack(_approved_gate(), GOOD_RULE_ID, 1).to_json()
    assert a == b == c
    body = json.loads(a)
    assert list(body) == sorted(body)


def test_export_writes_nothing(gate: AgentGate) -> None:
    head, n = gate.audit.head(), len(gate.decisions())
    build_evidence_pack(gate, GOOD_RULE_ID, 1)
    assert gate.audit.head() == head and len(gate.decisions()) == n


def test_pack_hash_detects_edits(gate: AgentGate) -> None:
    text = build_evidence_pack(gate, GOOD_RULE_ID, 1).to_json()
    pack, claimed = load_pack(text)
    assert pack.pack_sha256 == claimed
    edited, claimed2 = load_pack(text.replace('"status": "active"', '"status": "rejected"'))
    assert edited.pack_sha256 != claimed2


def test_segment_proof_catches_tampering(gate: AgentGate) -> None:
    pack = build_evidence_pack(gate, GOOD_RULE_ID, 1)
    entries = list(pack.audit.entries)
    head, n = pack.audit.proof.chain_head, pack.audit.proof.chain_entries
    assert verify_segment(entries, head, n, True).ok
    bad = entries[3].model_copy(update={"actor": "mallory"})
    edited = verify_segment([*entries[:3], bad, *entries[4:]], head, n, True)
    assert not edited.ok and "does not match its hash" in (edited.problem or "")
    gap = verify_segment([*entries[:3], *entries[4:]], head, n, True)
    assert not gap.ok and "gap" in (gap.problem or "")
    short = verify_segment(entries[:-1], head, n, True)
    assert not short.ok and "head" in (short.problem or "")
    assert not verify_segment([], head, n, True).ok
    assert not verify_segment(entries, head, n, False).ok


def test_rejected_rule_pack_shows_block() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(BAD_RULE_ID, 1)
    pack = build_evidence_pack(g, BAD_RULE_ID, 1)
    assert pack.status == "rejected" and pack.scorecard.verdict.value == "block"
    assert pack.approvals.policy is None and pack.canary_ramp is None
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    with pytest.raises(ValueError, match="no decisions"):
        build_evidence_pack(g, GOOD_RULE_ID, 1)


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() for p in reader.pages)


def test_pdf_parses_and_contains_key_fields(gate: AgentGate) -> None:
    pack = build_evidence_pack(gate, GOOD_RULE_ID, 1)
    data = render_pdf(pack)
    assert data.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 1
    assert reader.metadata is not None and GOOD_RULE_ID in (reader.metadata.title or "")
    text = _pdf_text(data)
    for needle in (
        f"{GOOD_RULE_ID}@v1",
        "Gate verdict: PASS",
        "Current status: active",
        pack.pack_sha256,
        pack.content_hash,
        pack.audit.proof.chain_head,
        "Proof: OK",
        "alice@demo.bank",
        "synthetic",
        "canary.ramp_completed",
    ):
        assert needle in text, needle


def test_pdf_is_deterministic(gate: AgentGate) -> None:
    pack = build_evidence_pack(gate, GOOD_RULE_ID, 1)
    assert render_pdf(pack) == render_pdf(pack)


def test_pdf_survives_non_latin1_text() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    spec = GOOD_RULE.model_copy(update={"description": "Contest delivered INR → ✓"})
    g.propose(GOOD_RULE_ID, spec, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    text = _pdf_text(render_pdf(build_evidence_pack(g, GOOD_RULE_ID, 1)))
    assert "Contest delivered INR" in text


def test_cli_evidence_and_run_with_evidence(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    g = _approved_gate(str(db))
    expected = build_evidence_pack(g, GOOD_RULE_ID, 1).to_json()
    out = tmp_path / "pack"
    toml = tmp_path / "gate.toml"
    toml.write_text(
        '[candidate]\nfile = "x.json"\n[gate]\nn_cases = 2000\n'
        f"bootstrap_resamples = 300\neval_seed = {SECRET_SEED}\n"
    )
    ev_args = ["ci", "evidence", "--db", str(db), GOOD_RULE_ID, "1", "--out", str(out)]
    assert main([*ev_args, "--config", str(toml)]) == 0
    assert (out / f"evidence-{GOOD_RULE_ID}-v1.json").read_text() == expected
    assert (out / f"evidence-{GOOD_RULE_ID}-v1.pdf").read_bytes().startswith(b"%PDF-")

    cfg = write_repo(tmp_path / "repo", GOOD)
    ev = tmp_path / "ev"
    args = ["ci", "run", "--config", str(cfg), "--out", str(tmp_path / "c.json")]
    assert main([*args, "--evidence", str(ev)]) == 0
    pack, claimed = load_pack((ev / f"evidence-{GOOD_RULE_ID}-v1.json").read_text())
    assert pack.pack_sha256 == claimed and pack.audit.proof.ok
    assert main(["ci", "evidence", "--db", str(db), "nope-rule", "1"]) == 2
