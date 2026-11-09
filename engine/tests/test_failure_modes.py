"""Failure modes: corrupted inputs, partial writes, and missing records."""

from __future__ import annotations

import pytest

from agent_gate.clock import SteppingClock
from agent_gate.domain import Action, RuleSpec
from agent_gate.gate import GateConfig, Outcome
from agent_gate.registry import Provenance, RuleStatus
from agent_gate.service import AgentGate
from agent_gate.sim import Simulator, generate_cases
from agent_gate.sim.ledger import JournalEntry, Posting
from agent_gate.sim.library import GOOD_RULE, GOOD_RULE_ID, C, baseline_policy
from agent_gate.storage import SQLiteStorage
from agent_gate.verifiers import verify_ledger

CFG = GateConfig(n_cases=1_000, bootstrap_resamples=200)
PROV = Provenance(agent_id="agent-1")


def _gate() -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    return g


def test_ledger_verifier_flags_injected_unbalanced_and_orphan_entries() -> None:
    cases = generate_cases(3, 200)
    run = Simulator(cases).run(baseline_policy())
    bogus = (
        JournalEntry(
            entry_id=9998,
            kind="fee",
            txn_id="t",
            case_id=cases[0].case_id,
            currency="USD",
            postings=(Posting(account="merchant:x", amount_minor=-5),),
        ),
        JournalEntry(
            entry_id=9999,
            kind="fee",
            txn_id="t",
            case_id="case-ghost",
            currency="USD",
            postings=(Posting(account="a", amount_minor=0),),
        ),
    )
    rep = verify_ledger(cases, run.model_copy(update={"journal": run.journal + bogus}))
    kinds = {f.kind for f in rep.findings}
    assert {"unbalanced_entry", "unknown_case", "conservation"} <= kinds


def test_hold_returns_rule_to_candidate_and_is_audited() -> None:
    g = _gate()
    never = RuleSpec(
        description="never fires",
        conditions=(C("mcc", "eq", "0000"),),
        action=Action.CONTEST,
        priority=60,
    )
    g.propose("never-fires", never, "ops", PROV)
    recs = g.evaluate("never-fires", 1)
    assert [r.decision.outcome for r in recs] == [Outcome.HOLD]
    assert g.registry.get("never-fires", 1).status is RuleStatus.CANDIDATE
    assert g.verify_integrity().ok


def test_failed_write_leaves_no_partial_state(monkeypatch: pytest.MonkeyPatch) -> None:
    g = _gate()
    entries_before = len(g.audit.entries())

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(g.storage, "append_audit", boom)
    with pytest.raises(RuntimeError):
        g.propose(GOOD_RULE_ID, GOOD_RULE, "ops", PROV)
    monkeypatch.undo()
    assert g.registry.versions(GOOD_RULE_ID) == []  # rule insert was rolled back too
    assert len(g.audit.entries()) == entries_before
    assert g.verify_integrity().ok


def test_integrity_detects_missing_records() -> None:
    g = _gate()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    s = g.storage
    s.unsafe_execute("DROP TRIGGER rule_versions_no_delete")  # type: ignore[attr-defined]
    s.unsafe_execute("DROP TRIGGER decisions_no_delete")  # type: ignore[attr-defined]
    s.unsafe_execute(  # type: ignore[attr-defined]
        "DELETE FROM rule_versions WHERE rule_id='base-fx-contest'"
    )
    s.unsafe_execute("DELETE FROM decisions WHERE decision_id='dec-00001'")  # type: ignore[attr-defined]
    s.unsafe_execute(  # type: ignore[attr-defined]
        "INSERT INTO rule_versions SELECT 'ghost-rule', version, content_hash, spec_json, "
        "owner, provenance_json, created_at, status, rollback_to FROM rule_versions "
        "WHERE rule_id='base-refund-contest'"
    )
    problems = " | ".join(g.verify_integrity().problems)
    assert "base-fx-contest@v1 in audit log but missing" in problems
    assert "decision dec-00001 in audit log but missing" in problems
    assert "ghost-rule@v1: exists in registry but never appears in audit log" in problems


def test_evaluate_refuses_non_candidates_and_unknown_rules() -> None:
    from agent_gate.registry import InvalidTransitionError, RuleNotFoundError

    g = _gate()
    with pytest.raises(InvalidTransitionError):
        g.evaluate("base-fx-contest", 1)  # already active
    with pytest.raises(RuleNotFoundError):
        g.evaluate("no-such-rule", 1)


def test_assurance_with_empty_policy_is_noop() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    assert g.run_assurance() is None
