"""The storage contract. Each backend must pass every test here unchanged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from scruple.audit.log import AuditLog
from scruple.audit.models import AuditEntry
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.registry import Provenance, RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import (
    AuditRepository,
    DecisionRepository,
    DuplicateKeyError,
    ImmutableHistoryError,
    RuleVersionRepository,
    Storage,
    Transactional,
)
from tests.helpers import baseline_versions, rv
from tests.storage.conftest import RawStorage

SPEC = GOOD_RULE


class BoomError(Exception):
    pass


def _audit(seq: int, payload: dict[str, object] | None = None) -> AuditEntry:
    return AuditEntry(
        seq=seq,
        at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
        event_type="x",
        actor="tester",
        payload=payload or {"i": seq},
        prev_hash="0" * 64,
        entry_hash=f"{seq:064x}",
    )


# --- interface ----------------------------------------------------------------


@pytest.mark.parametrize(
    "proto", [Storage, Transactional, RuleVersionRepository, AuditRepository, DecisionRepository]
)
def test_backend_implements_every_repository(store: RawStorage, proto: type) -> None:
    assert isinstance(store, proto)


def test_empty_store_reads(store: RawStorage) -> None:
    assert store.list_rule_versions() == []
    assert store.list_rule_versions("nope-rule") == []
    assert store.get_rule_version("nope-rule", 1) is None
    assert store.last_audit() is None
    assert store.list_audit() == []
    assert store.get_decision("dec-00001") is None
    assert store.list_decisions() == []
    assert store.count_decisions() == 0


# --- rule versions --------------------------------------------------------------


def test_rule_version_round_trips_exactly(store: RawStorage) -> None:
    prov = Provenance(
        agent_id="agent-7",
        episode_ids=("ep-1", "ep-2"),
        notes="unicode ok: é ✓",
        claimed_metrics={"win_rate": 0.1 + 0.2},
    )
    v = rv("r-one", SPEC).model_copy(update={"provenance": prov, "rollback_to": 3})
    store.insert_rule_version(v)
    assert store.get_rule_version("r-one", 1) == v
    assert store.list_rule_versions("r-one") == [v]


def test_non_utc_timestamps_round_trip(store: RawStorage) -> None:
    t = datetime(2026, 3, 4, 5, 6, 7, 891011, tzinfo=timezone(timedelta(hours=-5)))
    v = rv("r-tz", SPEC).model_copy(update={"created_at": t})
    store.insert_rule_version(v)
    got = store.get_rule_version("r-tz", 1)
    assert got is not None and got.created_at.isoformat() == t.isoformat()


def test_list_rule_versions_is_ordered(store: RawStorage) -> None:
    vs = baseline_versions()
    extra = [rv("aaa-first", SPEC, version=n) for n in (2, 1, 3)]
    for v in [*reversed(vs), *extra]:
        store.insert_rule_version(v)
    listed = store.list_rule_versions()
    assert [(v.rule_id, v.version) for v in listed] == sorted(
        (v.rule_id, v.version) for v in [*vs, *extra]
    )
    assert [v.version for v in store.list_rule_versions("aaa-first")] == [1, 2, 3]


def test_duplicate_rule_version_is_refused(store: RawStorage) -> None:
    store.insert_rule_version(rv("r-dup", SPEC))
    with pytest.raises(DuplicateKeyError):
        store.insert_rule_version(rv("r-dup", BAD_RULE))
    assert store.get_rule_version("r-dup", 1) == rv("r-dup", SPEC)


def test_status_and_rollback_pointer_are_mutable(store: RawStorage) -> None:
    store.insert_rule_version(rv("r-mut", SPEC))
    store.update_rule_status("r-mut", 1, RuleStatus.SHADOW)
    store.set_rollback_to("r-mut", 1, 7)
    got = store.get_rule_version("r-mut", 1)
    assert got == rv("r-mut", SPEC).model_copy(
        update={"status": RuleStatus.SHADOW, "rollback_to": 7}
    )
    store.set_rollback_to("r-mut", 1, None)
    got = store.get_rule_version("r-mut", 1)
    assert got is not None and got.rollback_to is None


def test_updates_touch_only_the_named_version(store: RawStorage) -> None:
    for n in (1, 2):
        store.insert_rule_version(rv("r-two", SPEC, version=n))
    store.insert_rule_version(rv("r-other", SPEC))
    store.update_rule_status("r-two", 2, RuleStatus.REJECTED)
    statuses = {(v.rule_id, v.version): v.status for v in store.list_rule_versions()}
    assert statuses == {
        ("r-other", 1): RuleStatus.CANDIDATE,
        ("r-two", 1): RuleStatus.CANDIDATE,
        ("r-two", 2): RuleStatus.REJECTED,
    }


def test_updating_a_missing_version_is_a_no_op(store: RawStorage) -> None:
    store.update_rule_status("ghost-rule", 1, RuleStatus.ACTIVE)
    store.set_rollback_to("ghost-rule", 1, 1)
    assert store.list_rule_versions() == []


# --- audit ------------------------------------------------------------------------


def test_audit_round_trips_and_orders(store: RawStorage) -> None:
    payload: dict[str, object] = {
        "nested": {"b": [1, 2.5, None, True], "a": "é ✓"},
        "float": 0.1 + 0.2,
        "big": 2**53 + 1,
    }
    entries = [_audit(1, payload), _audit(2), _audit(3)]
    for e in entries:
        store.append_audit(e)
    assert store.list_audit() == entries
    assert store.last_audit() == entries[-1]


def test_duplicate_audit_seq_is_refused(store: RawStorage) -> None:
    store.append_audit(_audit(1))
    with pytest.raises(DuplicateKeyError):
        store.append_audit(_audit(1, {"forged": True}))
    assert store.list_audit() == [_audit(1)]


def test_hash_chain_survives_a_round_trip(store: RawStorage) -> None:
    log = AuditLog(store, SteppingClock())
    for i in range(5):
        log.append("x", "tester", {"i": i, "f": i / 3, "s": "é"})
    assert log.verify().ok


# --- decisions --------------------------------------------------------------------


def test_decisions_round_trip_filter_and_count(store: RawStorage) -> None:
    store.insert_decision("dec-00002", "r-b", 1, '{"n": 2}')
    store.insert_decision("dec-00001", "r-a", 1, '{"n": 1}')
    store.insert_decision("dec-00003", "r-a", 2, '{"n": 3,  "spacing": "kept"}')
    assert store.get_decision("dec-00003") == '{"n": 3,  "spacing": "kept"}'
    assert store.list_decisions() == ['{"n": 1}', '{"n": 2}', '{"n": 3,  "spacing": "kept"}']
    assert store.list_decisions("r-a") == ['{"n": 1}', '{"n": 3,  "spacing": "kept"}']
    assert store.list_decisions("r-zzz") == []
    assert store.count_decisions() == 3


def test_duplicate_decision_is_refused(store: RawStorage) -> None:
    store.insert_decision("dec-00001", "r-a", 1, "{}")
    with pytest.raises(DuplicateKeyError):
        store.insert_decision("dec-00001", "r-a", 1, '{"forged": 1}')
    assert store.get_decision("dec-00001") == "{}"


# --- transactions -----------------------------------------------------------------


def test_transaction_commits(store: RawStorage) -> None:
    with store.transaction():
        store.insert_rule_version(rv("r-tx", SPEC))
        store.append_audit(_audit(1))
        store.insert_decision("dec-00001", "r-tx", 1, "{}")
    assert store.count_decisions() == 1 and len(store.list_audit()) == 1


def test_failed_transaction_rolls_back_every_aggregate(store: RawStorage) -> None:
    store.append_audit(_audit(1))
    with pytest.raises(BoomError), store.transaction():  # noqa: PT012
        store.insert_rule_version(rv("r-tx", SPEC))
        store.append_audit(_audit(2))
        store.insert_decision("dec-00001", "r-tx", 1, "{}")
        store.update_rule_status("r-tx", 1, RuleStatus.ACTIVE)
        raise BoomError
    assert store.list_rule_versions() == []
    assert store.list_audit() == [_audit(1)]
    assert store.count_decisions() == 0


def test_nested_transactions_commit_only_at_the_outer_level(store: RawStorage) -> None:
    with pytest.raises(BoomError), store.transaction():  # noqa: PT012
        with store.transaction():
            store.insert_rule_version(rv("r-inner", SPEC))
        assert store.get_rule_version("r-inner", 1) is not None  # visible inside
        raise BoomError
    assert store.get_rule_version("r-inner", 1) is None


def test_inner_failure_rolls_back_the_outer_transaction(store: RawStorage) -> None:
    with pytest.raises(BoomError), store.transaction():  # noqa: PT012
        store.insert_rule_version(rv("r-outer", SPEC))
        with store.transaction():
            store.append_audit(_audit(1))
            raise BoomError
    assert store.list_rule_versions() == [] and store.list_audit() == []


def test_store_is_usable_after_a_failed_transaction(store: RawStorage) -> None:
    with pytest.raises(BoomError), store.transaction():
        raise BoomError
    store.insert_decision("dec-00001", "r-a", 1, "{}")
    assert store.count_decisions() == 1


def test_duplicate_inside_transaction_rolls_back_cleanly(store: RawStorage) -> None:
    store.insert_decision("dec-00001", "r-a", 1, "{}")
    with pytest.raises(DuplicateKeyError), store.transaction():  # noqa: PT012
        store.append_audit(_audit(1))
        store.insert_decision("dec-00001", "r-a", 1, "{}")
    assert store.list_audit() == []
    store.append_audit(_audit(1))
    assert store.list_audit() == [_audit(1)]


# --- append-only, enforced by the database itself ---------------------------------


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE audit_events SET actor='mallory'", "append-only"),
        ("UPDATE audit_events SET payload_json='{}'", "append-only"),
        ("DELETE FROM audit_events", "append-only"),
        ("UPDATE decisions SET body_json='{}'", "append-only"),
        ("UPDATE decisions SET rule_id='r-x'", "append-only"),
        ("DELETE FROM decisions", "append-only"),
        ("UPDATE rule_versions SET spec_json='{}'", "immutable"),
        ("UPDATE rule_versions SET owner='mallory'", "immutable"),
        ("UPDATE rule_versions SET content_hash='x'", "immutable"),
        ("UPDATE rule_versions SET provenance_json='{}'", "immutable"),
        ("UPDATE rule_versions SET created_at='2020-01-01T00:00:00+00:00'", "immutable"),
        ("UPDATE rule_versions SET version=9", "immutable"),
        ("UPDATE rule_versions SET rule_id='r-x'", "immutable"),
        ("DELETE FROM rule_versions", "immutable"),
    ],
)
def test_database_refuses_to_rewrite_history(store: RawStorage, sql: str, message: str) -> None:
    store.insert_rule_version(rv("r-hist", SPEC))
    store.append_audit(_audit(1))
    store.insert_decision("dec-00001", "r-hist", 1, '{"n": 1}')
    with pytest.raises(ImmutableHistoryError, match=message):
        store.unsafe_execute(sql)
    assert store.list_rule_versions() == [rv("r-hist", SPEC)]
    assert store.list_audit() == [_audit(1)]
    assert store.list_decisions() == ['{"n": 1}']


def test_database_allows_status_changes(store: RawStorage) -> None:
    store.insert_rule_version(rv("r-hist", SPEC))
    store.unsafe_execute("UPDATE rule_versions SET status='shadow', rollback_to=2")
    got = store.get_rule_version("r-hist", 1)
    assert got is not None and got.status is RuleStatus.SHADOW and got.rollback_to == 2


# --- the whole service on this backend --------------------------------------------


def test_service_end_to_end(store: RawStorage) -> None:
    g = AgentGate(
        store,
        SteppingClock(),
        GateConfig(n_cases=600, bootstrap_resamples=100, min_affected_cases=5),
    )
    assert len(g.bootstrap_baseline()) == 6
    assert g.bootstrap_baseline() == []  # idempotent
    prov = Provenance(agent_id="agent-1")
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", prov)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", prov)
    outcomes = [r.decision.outcome.value for r in g.evaluate(BAD_RULE_ID, 1)]
    assert outcomes == ["block"]
    rep = g.verify_integrity()
    assert rep.ok, rep.problems
    assert store.count_decisions() == 1
    assert g.registry.get(BAD_RULE_ID, 1).status is RuleStatus.REJECTED
