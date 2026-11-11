"""A decision re-sent for a case whose events retention purged is refused, not counted
twice: the purge keeps each decision's key hash (only the hash) and `record` checks it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from scruple.service import AgentGate
from scruple.storage import ImmutableHistoryError
from scruple.storage.imported_shadow import decision_key, repository_for
from scruple.storage.retention import RetentionPolicy, apply_retention, set_policy
from tests.storage.conftest import RawStorage
from tests.storage.test_retention import BATCH, T0, Clock, _shadow


def _purged(store: RawStorage) -> Clock:
    """t1 records BATCH[:10] at T0 and BATCH[10:20] at T0+40d; a 30-day policy applied at
    T0+45d purges the first batch."""
    clock = Clock()
    gate = AgentGate(store, clock)
    _shadow(store, clock).record(BATCH[:10])
    clock.t = T0 + timedelta(days=40)
    _shadow(store, clock).record(BATCH[10:20])
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=30), "ops")
    clock.t = T0 + timedelta(days=45)
    [p] = apply_retention(gate)
    assert p.rows == 1
    return clock


def test_resending_a_purged_decision_is_refused(store: RawStorage) -> None:
    clock = _purged(store)
    fresh = _shadow(store, clock)  # a new process: rebuilt from what is left
    before = fresh.summary()
    history = fresh.history()
    assert before.decisions == 10
    with pytest.raises(ValueError, match="purged by retention"):
        fresh.record(BATCH[:10])
    with pytest.raises(ValueError, match="purged by retention"):
        fresh.record([BATCH[3], *BATCH[20:25]])  # all or nothing
    assert fresh.summary() == before and fresh.history() == history
    assert fresh.record(BATCH[20:25]).total_recorded == 15  # new cases still go in
    with pytest.raises(ValueError, match="already recorded"):
        fresh.record(BATCH[10:12])  # not purged: the recorder itself refuses


def test_only_key_hashes_are_kept(store: RawStorage) -> None:
    _purged(store)
    repo = repository_for(store)
    ids = [d.case.case_id for d in BATCH[:10]]
    keys = [decision_key("t1", "r-ret", 1, c) for c in ids]
    assert repo.purged_keys("t1", "r-ret", 1, keys) == set(keys)
    assert repo.purged_keys("t1", "r-ret", 1, [decision_key("t1", "r-ret", 1, "x")]) == set()
    rows = store.connection.query("SELECT * FROM imported_shadow_keys")  # type: ignore[attr-defined]
    assert len(rows) == 10
    text = repr(rows)
    assert not any(c in text for c in ids)


def test_keys_are_scoped_to_tenant_rule_and_version(store: RawStorage) -> None:
    clock = _purged(store)
    assert _shadow(store, clock, "t2").record(BATCH[:10]).recorded == 10  # other tenant
    repo = repository_for(store)
    k = decision_key("t1", "r-ret", 1, BATCH[0].case.case_id)
    assert repo.purged_keys("t1", "r-ret", 2, [k]) == set()
    assert repo.purged_keys("t2", "r-ret", 1, [k]) == set()


def test_a_second_purge_and_the_table_stay_append_only(store: RawStorage) -> None:
    clock = _purged(store)
    gate = AgentGate(store, clock)
    clock.t = T0 + timedelta(days=200)
    apply_retention(gate)  # purges the second batch too
    assert len(store.connection.query("SELECT key_sha256 FROM imported_shadow_keys")) == 20  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="purged by retention"):
        _shadow(store, clock).record(BATCH[15:16])
    with pytest.raises(ImmutableHistoryError):
        store.unsafe_execute("DELETE FROM imported_shadow_keys")
    with pytest.raises(ImmutableHistoryError):
        store.unsafe_execute("UPDATE imported_shadow_keys SET seq = 0")
