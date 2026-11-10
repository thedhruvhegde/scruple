"""Retention: row-level case data goes after N days; aggregates and the audit chain stay."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.domain import Action
from scruple.gate.shadow_imported import ImportedShadowRecorder
from scruple.ingest.models import IngestBatch
from scruple.ingest.store import CaseStore
from scruple.service import AgentGate
from scruple.storage import ImmutableHistoryError
from scruple.storage.imported_shadow import DurableImportedShadow, repository_for
from scruple.storage.retention import (
    EVENT_POLICY,
    EVENT_PURGED,
    RetentionPolicy,
    apply_retention,
    digest,
    retention_for,
    set_policy,
)
from tests.storage.conftest import RawStorage
from tests.test_gate_shadow_imported import Always, decisions, synthetic

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ROWS = synthetic(30)
BATCH = decisions(ROWS, as_opened=True)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t


def _shadow(storage: RawStorage, clock: Clock, tenant: str = "t1") -> DurableImportedShadow:
    return DurableImportedShadow(
        repository_for(storage),
        "r-ret",
        1,
        lambda: ImportedShadowRecorder(Always(Action.ACCEPT), synthetic=True),
        tenant_id=tenant,
        clock=clock,
    )


def test_policy_validation() -> None:
    with pytest.raises(ValidationError):
        RetentionPolicy(tenant_id="t1", days=0)
    with pytest.raises(ValidationError):
        RetentionPolicy(tenant_id="bad tenant!", days=5)


def test_latest_policy_wins_and_is_audited(store: RawStorage) -> None:
    gate = AgentGate(store, SteppingClock())
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=90), "ops")
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=30), "ops")
    set_policy(gate, RetentionPolicy(tenant_id="t2", days=7), "ops")
    assert retention_for(store).policies() == [
        RetentionPolicy(tenant_id="t1", days=30),
        RetentionPolicy(tenant_id="t2", days=7),
    ]
    assert [e.event_type for e in gate.audit.entries()] == [EVENT_POLICY] * 3


def test_purge_deletes_old_rows_keeps_aggregates_and_the_chain(store: RawStorage) -> None:
    clock = Clock()
    gate = AgentGate(store, clock)
    old, keep, other = _shadow(store, clock), _shadow(store, clock), _shadow(store, clock, "t2")
    old.record(BATCH[:10])
    clock.t = T0 + timedelta(days=40)
    keep.record(BATCH[10:20])  # catches up on the old batch first
    other.record(BATCH[:5])  # another tenant, with no policy
    history_before = [s.body_json for s in old.history()]
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=30), "ops")
    head_before = gate.audit.head()

    clock.t = T0 + timedelta(days=45)
    [purge] = apply_retention(gate)
    assert (purge.tenant_id, purge.table, purge.rows) == ("t1", "imported_shadow_events", 1)
    assert purge.cutoff == T0 + timedelta(days=15)

    # Row-level data is gone; the summaries recorded at the time are all still there.
    remaining = repository_for(store).events_after("t1", "r-ret", 1, 0)
    assert [e.seq for e in remaining] == [2]
    assert [s.body_json for s in _shadow(store, clock).history()] == history_before
    assert _shadow(store, clock).summary().decisions == 10  # rebuilt from what is left
    assert old.summary().decisions == 10  # a long-lived recorder notices the purge too
    assert keep.record(BATCH[20:25]).total_recorded == 15  # and numbering moves on
    assert _shadow(store, clock, "t2").summary().decisions == 5  # untouched

    # The audit chain is intact, verifies, and records the purge with its digest.
    chain = gate.audit.verify()
    assert chain.ok and gate.verify_integrity().ok
    entries = gate.audit.entries()
    assert entries[-2].entry_hash == head_before
    last = entries[-1]
    assert last.event_type == EVENT_PURGED and last.seq == purge.audit_seq
    assert last.payload["rows"] == 1 and last.payload["digest_sha256"] == purge.digest_sha256
    assert retention_for(store).purges("t1")[0]["digest_sha256"] == purge.digest_sha256

    # Applying again deletes nothing and records nothing.
    assert apply_retention(gate) == []
    assert len(gate.audit.entries()) == len(entries)


def test_the_append_only_trigger_still_holds_outside_a_purge(store: RawStorage) -> None:
    clock = Clock()
    gate = AgentGate(store, clock)
    _shadow(store, clock).record(BATCH[:3])
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=1), "ops")
    clock.t = T0 + timedelta(days=3)
    assert apply_retention(gate)[0].rows == 1
    _shadow(store, clock).record(BATCH[3:6])
    for sql in (
        "DELETE FROM imported_shadow_events",
        "DELETE FROM imported_shadow_summaries",
        "DELETE FROM audit_events",
        "DELETE FROM retention_purges",
        "UPDATE retention_policies SET days = 9999",
    ):
        with pytest.raises(ImmutableHistoryError):
            store.unsafe_execute(sql)


def test_digest_commits_to_exactly_the_deleted_rows() -> None:
    assert digest(["a", "b"]) == digest(["b", "a"]) != digest(["a"])


# --- the case store -------------------------------------------------------------------


def test_case_store_purge(tmp_path: Path, store: RawStorage) -> None:
    clock = Clock()
    gate = AgentGate(store, clock)
    cs = CaseStore(tmp_path / "cases.db")
    cases = [c for c, _ in ROWS]
    cutoff_case = sorted(c.opened_at for c in cases)[len(cases) // 2]
    for i, (c, mv) in enumerate(ROWS):
        cs.write_webhook(
            source="stripe",
            event_id=f"evt_{i}",
            event_type="charge.dispute.created",
            event_at=c.opened_at,
            payload_sha256="0" * 64,
            batch=IngestBatch(cases=(c,), movements=mv),
            tenant_id="t1",
        )
    runs_before = cs.runs()
    events_before = cs.webhook_events()
    days = 10
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=days), "ops")
    clock.t = cutoff_case + timedelta(days=days)
    out = apply_retention(gate, case_stores={"t1": cs})
    gone = {c.case_id for c in cases if c.opened_at < cutoff_case}
    by_table = {p.table: p for p in out}
    assert by_table["cases"].rows == len(gone) > 0
    assert by_table["webhook_case_events"].rows == len(gone)
    assert {sc.case.case_id for sc in cs.cases()} == {c.case_id for c in cases} - gone
    assert all(m.case_external_id not in {g.split(":", 1)[1] for g in gone} for m in cs.movements())
    # Aggregates stay: run reports and webhook ids (so redeliveries are still refused).
    assert cs.runs() == runs_before and cs.webhook_events() == events_before
    redeliver = ROWS[0]
    again = cs.write_webhook(
        source="stripe",
        event_id="evt_0",
        event_type="charge.dispute.created",
        event_at=redeliver[0].opened_at,
        payload_sha256="0" * 64,
        batch=IngestBatch(cases=(redeliver[0],)),
        tenant_id="t1",
    )
    assert again.duplicate
    [meta] = cs.purges()
    assert meta["tables"]["cases"]["digest_sha256"] == by_table["cases"].digest_sha256
    purged = [e for e in gate.audit.entries() if e.event_type == EVENT_PURGED]
    assert {e.payload["table"] for e in purged} == {"cases", "movements", "webhook_case_events"}
    assert gate.audit.verify().ok
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        cs._db.execute("DELETE FROM cases")
    json.dumps(meta)  # plain JSON


def test_retention_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    db = str(tmp_path / "gate.db")
    assert main(["retention", "set", "--db", db, "--tenant", "t1", "--days", "30"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"tenant_id": "t1", "days": 30}]
    assert main(["retention", "apply", "--db", db, "--tenant", "t1"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["retention", "set", "--db", db, "--tenant", "t1", "--days", "0"]) == 2
