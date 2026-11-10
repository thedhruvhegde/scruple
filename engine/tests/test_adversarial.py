"""Every verifier is attacked. An attack must fool a naive verifier (so it is a real
attack) and must not get past the gate. See docs/VERIFIER_FINDINGS.md."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scruple.domain import Action, Condition, RuleSpec
from scruple.gate import GateConfig, Outcome, evaluate_shadow
from scruple.gate.redteam import run_rule_attack, run_tamper_attacks
from scruple.registry import DuplicateContentError, Provenance, Registry
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, C
from scruple.verifiers.adversarial import rule_attacks
from tests.helpers import baseline_versions, rv

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=500)


@pytest.mark.parametrize("attack", rule_attacks(), ids=lambda a: a.name)
def test_rule_attack_fools_naive_verifier_but_not_gate(attack: object) -> None:
    from scruple.verifiers.adversarial import RuleAttack

    assert isinstance(attack, RuleAttack)
    result = run_rule_attack(attack, CFG)
    assert result.naive_fooled, f"{attack.name} is not a real attack against the strawman"
    assert result.defended, f"{attack.name} got past the gate: {result.detail}"


def test_tampering_attacks_are_detected() -> None:
    results = run_tamper_attacks(CFG)
    assert results
    for r in results:
        assert r.naive_fooled, r.name
        assert r.defended, r.name


def test_rules_cannot_reference_ground_truth_or_identifiers() -> None:
    for forbidden in ("claim_valid", "scenario", "case_id", "txn_id", "merchant_id"):
        with pytest.raises(ValidationError):
            Condition.model_validate({"field": forbidden, "op": "eq", "value": True})


def test_condition_type_confusion_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Condition.model_validate({"field": "amount_usd_minor", "op": "gt", "value": True})
    with pytest.raises(ValidationError):
        Condition.model_validate({"field": "three_ds", "op": "gt", "value": 1})
    with pytest.raises(ValidationError):
        Condition.model_validate({"field": "reason_code", "op": "eq", "value": "made_up"})


def test_self_reported_metrics_are_ignored() -> None:
    cand = rv(BAD_RULE_ID, BAD_RULE).model_copy(
        update={
            "provenance": Provenance(
                agent_id="agent-7", claimed_metrics={"improvement_usd": 1e9, "accuracy": 1.0}
            )
        }
    )
    assert evaluate_shadow(cand, baseline_versions(), CFG).outcome is Outcome.BLOCK


def test_rewording_a_rule_does_not_buy_a_new_evaluation(registry: Registry) -> None:
    prov = Provenance(agent_id="agent-7")
    registry.register_candidate("inr", GOOD_RULE, owner="ops", provenance=prov)
    reworded = GOOD_RULE.model_copy(
        update={
            "description": "totally new insight",
            "conditions": tuple(reversed(GOOD_RULE.conditions)),
        }
    )
    with pytest.raises(DuplicateContentError):
        registry.register_candidate("inr", reworded, owner="ops", provenance=prov)


def test_priority_override_is_surfaced_as_conflict() -> None:
    v = evaluate_shadow(rv(BAD_RULE_ID, BAD_RULE), baseline_versions(), CFG)
    assert v.shadow is not None
    overridden = {c.overridden_rule for c in v.shadow.conflicts}
    assert "base-late-evidence-accept@v1" in overridden
    assert "base-high-value-escalate@v1" not in overridden  # priority 0 still wins


def test_escalate_everything_is_blocked_by_escalation_guard() -> None:
    spec = RuleSpec(
        description="escalate",
        conditions=(C("amount_usd_minor", "ge", 0),),
        action=Action.ESCALATE,
        priority=1,
    )
    v = evaluate_shadow(rv("esc", spec), baseline_versions(), CFG)
    assert any(c.name == "escalation_rate_guard" and not c.passed for c in v.checks)


def test_memorised_outcomes_are_caught_by_the_heldout_gap() -> None:
    """Was G-8 in test_known_gaps.py: X33 (fingerprint the case, replay the memorised
    winning move) leaves a clean episode, but its advantage over the baseline exists
    only on cases it could have seen. MEM-01 compares it with held-out probes."""
    from scruple.verifiers.exploits import EXPLOIT_BY_ID, run_exploit

    run = run_exploit(EXPLOIT_BY_ID["X33"], range(120))
    assert run.fooled and run.naive_exploit > run.naive_honest
    assert run.status == "caught" and run.fired_on == run.exercised > 0
    assert run.gate_rate == 1.0  # every episode still passes every per-episode check
    mem = run.cards.get("MEM-01")
    assert not mem.passed and "exceeds held-out" in mem.explanation
