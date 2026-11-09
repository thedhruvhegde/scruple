from __future__ import annotations

import pytest

from scruple.audit.log import AuditLog, verify_entries
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

CFG = GateConfig(n_cases=1_000, bootstrap_resamples=300, min_affected_cases=10)


def _populated(path: str) -> AgentGate:
    g = AgentGate.open(path, SteppingClock(), CFG)
    g.bootstrap_baseline()
    prov = Provenance(agent_id="agent-1")
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", prov)
    g.evaluate(GOOD_RULE_ID, 1)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", prov)
    g.evaluate(BAD_RULE_ID, 1)
    return g


def test_chain_verifies_when_untouched(audit: AuditLog) -> None:
    for i in range(5):
        audit.append("x", "tester", {"i": i})
    res = audit.verify()
    assert res.ok and res.entries == 5 and res.head_hash == audit.head()


def test_db_triggers_refuse_edits(storage: SQLiteStorage, audit: AuditLog) -> None:
    audit.append("x", "tester", {"i": 1})
    with pytest.raises(Exception, match="append-only"):
        storage.unsafe_execute("UPDATE audit_events SET actor='mallory'")
    with pytest.raises(Exception, match="append-only"):
        storage.unsafe_execute("DELETE FROM audit_events")


@pytest.mark.parametrize("attack", ["edit_payload", "delete_middle", "reorder", "rehash_one"])
def test_tampering_is_detected(audit: AuditLog, attack: str) -> None:
    for i in range(6):
        audit.append("x", "tester", {"i": i})
    es = audit.entries()
    if attack == "edit_payload":
        es[2] = es[2].model_copy(update={"payload": {"i": 999}})
    elif attack == "delete_middle":
        del es[3]
    elif attack == "reorder":
        es[1], es[2] = es[2], es[1]
    elif attack == "rehash_one":  # attacker recomputes the edited entry's own hash
        from scruple.audit.log import compute_hash

        e = es[2].model_copy(update={"payload": {"i": 999}})
        es[2] = e.model_copy(update={"entry_hash": compute_hash(e)})
    assert not verify_entries(es).ok


def test_failure_points_at_the_position_not_the_claimed_seq(audit: AuditLog) -> None:
    """Regression (found by tests/test_audit_tamper.py): after deleting entry 3, or
    swapping entries 3 and 4, verification reported seq 4 as the first bad entry,
    which implied entry 3 was fine when it was the one missing or moved."""
    for i in range(6):
        audit.append("x", "tester", {"i": i})
    es = audit.entries()
    deleted = es[:2] + es[3:]
    assert verify_entries(deleted).first_bad_seq == 3
    swapped = [*es[:2], es[3], es[2], *es[4:]]
    assert verify_entries(swapped).first_bad_seq == 3


def test_truncation_needs_anchored_head(audit: AuditLog) -> None:
    for i in range(4):
        audit.append("x", "tester", {"i": i})
    head = audit.head()
    truncated = audit.entries()[:-1]
    assert verify_entries(truncated).ok  # internally consistent...
    assert not verify_entries(truncated, expected_head=head).ok  # ...but not the anchor


def test_raw_file_edit_of_audit_log_is_detected(tmp_path: object) -> None:
    path = f"{tmp_path}/gate.db"
    g = _populated(path)
    assert g.verify_integrity().ok
    # An insider with file access drops the trigger and rewrites history.
    g.storage.unsafe_execute("DROP TRIGGER audit_no_update")  # type: ignore[attr-defined]
    g.storage.unsafe_execute(  # type: ignore[attr-defined]
        "UPDATE audit_events SET payload_json = replace(payload_json, 'block', 'promote') "
        "WHERE event_type = 'decision.recorded'"
    )
    rep = g.verify_integrity()
    assert not rep.ok and "edited" in (rep.chain.problem or "")


def test_raw_edit_of_rule_status_is_detected(tmp_path: object) -> None:
    g = _populated(f"{tmp_path}/gate.db")
    # Someone flips the blocked rule to active directly in the database.
    g.storage.unsafe_execute(  # type: ignore[attr-defined]
        f"UPDATE rule_versions SET status='active' WHERE rule_id='{BAD_RULE_ID}'"
    )
    rep = g.verify_integrity()
    assert not rep.ok
    assert any("audit log says rejected" in p for p in rep.problems)


def test_raw_edit_of_decision_is_detected(tmp_path: object) -> None:
    g = _populated(f"{tmp_path}/gate.db")
    g.storage.unsafe_execute("DROP TRIGGER decisions_no_update")  # type: ignore[attr-defined]
    g.storage.unsafe_execute(  # type: ignore[attr-defined]
        "UPDATE decisions SET body_json = replace(body_json, 'Blocked', 'Approved')"
    )
    rep = g.verify_integrity()
    assert any("modified after it was recorded" in p for p in rep.problems)
