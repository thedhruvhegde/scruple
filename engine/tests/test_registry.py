from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_gate.audit.log import AuditLog
from agent_gate.domain import Action, Condition, RuleSpec
from agent_gate.hashing import content_hash
from agent_gate.registry import (
    DuplicateContentError,
    InvalidTransitionError,
    Provenance,
    Registry,
    RuleStatus,
    RuleVersion,
)
from agent_gate.storage import SQLiteStorage


def spec(priority: int = 50, action: Action = Action.CONTEST) -> RuleSpec:
    return RuleSpec(
        description="contest not-received with delivery confirmation",
        conditions=(
            Condition(field="reason_code", op="eq", value="not_received"),
            Condition(field="delivery_confirmed", op="eq", value=True),
        ),
        action=action,
        priority=priority,
    )


PROV = Provenance(agent_id="agent-7", episode_ids=("ep-1", "ep-2"))


def test_register_candidate_assigns_version_and_hash(registry: Registry) -> None:
    rv = registry.register_candidate("inr-contest", spec(), owner="ops-team", provenance=PROV)
    assert rv.version == 1
    assert rv.status is RuleStatus.CANDIDATE
    assert rv.content_hash == content_hash(spec())
    rv2 = registry.register_candidate(
        "inr-contest", spec(priority=60), owner="ops-team", provenance=PROV
    )
    assert rv2.version == 2


def test_content_hash_is_stable_and_order_sensitive() -> None:
    a = spec()
    b = RuleSpec(
        description=a.description, conditions=a.conditions, action=a.action, priority=a.priority
    )
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(spec(priority=51))


def test_resubmitting_identical_content_is_refused(registry: Registry) -> None:
    registry.register_candidate("inr-contest", spec(), owner="ops", provenance=PROV)
    with pytest.raises(DuplicateContentError):
        registry.register_candidate("inr-contest", spec(), owner="ops", provenance=PROV)


def test_happy_path_state_machine(registry: Registry) -> None:
    registry.register_candidate("r-one", spec(), owner="ops", provenance=PROV)
    registry.transition("r-one", 1, RuleStatus.SHADOW, actor="gate", reason="start")
    registry.transition("r-one", 1, RuleStatus.CANARY, actor="gate", reason="shadow passed")
    active = registry.activate("r-one", 1, actor="risk", reason="approved")
    assert active.status is RuleStatus.ACTIVE
    assert [r.rule_id for r in registry.active_rules()] == ["r-one"]


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ([], RuleStatus.ACTIVE),  # candidate cannot skip straight to active
        ([], RuleStatus.CANARY),
        ([RuleStatus.SHADOW], RuleStatus.ACTIVE),
        ([RuleStatus.REJECTED], RuleStatus.SHADOW),  # rejected is terminal
    ],
)
def test_illegal_transitions_are_refused(
    registry: Registry, path: list[RuleStatus], bad: RuleStatus
) -> None:
    registry.register_candidate("r-two", spec(), owner="ops", provenance=PROV)
    for s in path:
        registry.transition("r-two", 1, s, actor="gate", reason="x")
    with pytest.raises(InvalidTransitionError):
        registry.transition("r-two", 1, bad, actor="gate", reason="x")


def test_retired_cannot_be_reactivated_except_by_rollback(registry: Registry) -> None:
    _activate(registry, "r-three", spec())
    _activate(registry, "r-three", spec(priority=40))
    with pytest.raises(InvalidTransitionError):
        registry.transition("r-three", 1, RuleStatus.ACTIVE, actor="x", reason="sneaky")


def test_activation_retires_previous_and_sets_rollback_pointer(registry: Registry) -> None:
    _activate(registry, "r-four", spec())
    v2 = _activate(registry, "r-four", spec(priority=40))
    assert v2.rollback_to == 1
    assert registry.get("r-four", 1).status is RuleStatus.RETIRED
    assert [(r.rule_id, r.version) for r in registry.active_rules()] == [("r-four", 2)]


def test_rollback_restores_previous_version(registry: Registry) -> None:
    _activate(registry, "r-five", spec())
    _activate(registry, "r-five", spec(priority=40))
    retired, restored = registry.rollback("r-five", actor="risk", reason="regression")
    assert retired.version == 2 and retired.status is RuleStatus.RETIRED
    assert restored is not None and restored.version == 1
    assert restored.status is RuleStatus.ACTIVE


def test_rollback_of_first_version_removes_rule_from_policy(registry: Registry) -> None:
    _activate(registry, "r-six", spec())
    _, restored = registry.rollback("r-six", actor="risk", reason="bad")
    assert restored is None
    assert registry.active_rules() == []


def test_rollback_without_active_version_fails(registry: Registry) -> None:
    registry.register_candidate("r-seven", spec(), owner="ops", provenance=PROV)
    with pytest.raises(InvalidTransitionError):
        registry.rollback("r-seven", actor="risk", reason="nothing active")


def test_every_change_is_in_the_audit_log(registry: Registry, audit: AuditLog) -> None:
    _activate(registry, "r-eight", spec())
    registry.rollback("r-eight", actor="risk", reason="drill")
    types = [e.event_type for e in audit.entries() if e.payload.get("rule_id") == "r-eight"]
    assert types == [
        "rule.registered",
        "rule.status_changed",
        "rule.status_changed",
        "rule.activated",
        "rule.rolled_back",
    ]
    assert audit.verify().ok


def test_storage_rejects_mutating_rule_content(registry: Registry, storage: SQLiteStorage) -> None:
    registry.register_candidate("r-nine", spec(), owner="ops", provenance=PROV)
    with pytest.raises(Exception, match="immutable"):
        storage.unsafe_execute("UPDATE rule_versions SET spec_json = '{}' WHERE rule_id = 'r-nine'")


def test_rule_ids_are_validated(registry: Registry) -> None:
    with pytest.raises(ValidationError):
        registry.register_candidate("Bad ID!", spec(), owner="ops", provenance=PROV)


def test_import_baseline_is_active_and_audited(registry: Registry, audit: AuditLog) -> None:
    rv = registry.import_baseline("base-x", spec(), owner="ops", actor="admin")
    assert rv.status is RuleStatus.ACTIVE
    assert audit.entries()[-1].event_type == "rule.baseline_imported"


def _activate(registry: Registry, rule_id: str, s: RuleSpec) -> RuleVersion:
    rv = registry.register_candidate(rule_id, s, owner="ops", provenance=PROV)
    registry.transition(rule_id, rv.version, RuleStatus.SHADOW, actor="gate", reason="t")
    registry.transition(rule_id, rv.version, RuleStatus.CANARY, actor="gate", reason="t")
    return registry.activate(rule_id, rv.version, actor="risk", reason="t")
