"""Tenancy in storage: every row has a tenant, every query is scoped, chains are per tenant."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.auth import models as auth_models
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage, sql
from tests.ci_support import PROV
from tests.helpers import rv
from tests.storage.conftest import RawStorage, pg_store, store  # noqa: F401  (fixtures)
from tests.storage.test_contract import _audit


def test_tenant_constants_match_auth() -> None:
    assert sql.DEFAULT_TENANT == auth_models.DEFAULT_TENANT
    assert sql.TENANT_PATTERN == auth_models.TENANT_PATTERN


@pytest.mark.parametrize("bad", ["", "A", "Bank", "a", "-x", "a b", "x" * 64, "acme_bank"])
def test_invalid_tenant_ids_are_refused(bad: str) -> None:
    s = SQLiteStorage(":memory:")
    with pytest.raises(ValueError, match="tenant"):
        s.for_tenant(bad)
    assert not re.match(auth_models.TENANT_PATTERN, bad)


def test_default_tenant_and_views(store: RawStorage) -> None:  # noqa: F811
    assert store.tenant == "default"
    a = store.for_tenant("bank-a")
    assert a.tenant == "bank-a" and a.for_tenant("bank-a") is a
    assert store.for_tenant("default") is store


def test_same_keys_in_two_tenants_never_collide(store: RawStorage) -> None:  # noqa: F811
    a = store.for_tenant("bank-a")
    b = store.for_tenant("bank-b")
    for s, spec in ((a, GOOD_RULE), (b, BAD_RULE)):
        s.insert_rule_version(rv("r-shared", spec))
        s.append_audit(_audit(1, {"who": s.tenant}))
        s.insert_decision("dec-00001", "r-shared", 1, f'{{"t": "{s.tenant}"}}')
    a.update_rule_status("r-shared", 1, RuleStatus.SHADOW)
    ra, rb = a.get_rule_version("r-shared", 1), b.get_rule_version("r-shared", 1)
    assert ra is not None and rb is not None
    assert ra.status is RuleStatus.SHADOW and rb.status is RuleStatus.CANDIDATE
    assert ra.spec == GOOD_RULE and rb.spec == BAD_RULE
    assert a.get_decision("dec-00001") == '{"t": "bank-a"}'
    assert b.list_decisions() == ['{"t": "bank-b"}']
    assert [e.payload for e in a.list_audit()] == [{"who": "bank-a"}]
    last = b.last_audit()
    assert last is not None and last.payload == {"who": "bank-b"}
    assert a.count_decisions() == b.count_decisions() == 1
    assert store.list_rule_versions() == [] and store.count_decisions() == 0


def test_tenant_id_cannot_be_rewritten(store: RawStorage) -> None:  # noqa: F811
    a = store.for_tenant("bank-a")
    a.insert_rule_version(rv("r-move", GOOD_RULE))
    a.append_audit(_audit(1))
    a.insert_decision("dec-00001", "r-move", 1, "{}")
    for table in ("rule_versions", "audit_events", "decisions"):
        with pytest.raises(ImmutableHistoryError):
            store.unsafe_execute(f"UPDATE {table} SET tenant_id='bank-b'")
    assert store.for_tenant("bank-b").list_rule_versions() == []


OPS = st.lists(
    st.tuples(
        st.sampled_from(["bank-a", "bank-b", "default"]),
        st.sampled_from(["rule", "audit", "decision"]),
        st.sampled_from(["r-one", "r-two", "r-three"]),
    ),
    max_size=25,
)


@settings(max_examples=30)
@given(OPS)
def test_each_tenant_sees_exactly_its_own_writes(ops: list[tuple[str, str, str]]) -> None:
    s = SQLiteStorage(":memory:")
    model: dict[str, dict[str, list[str]]] = {}
    for tenant, kind, rid in ops:
        v = s.for_tenant(tenant)
        m = model.setdefault(tenant, {"rule": [], "audit": [], "decision": []})
        if kind == "rule":
            version = 1 + sum(1 for r in m["rule"] if r == rid)
            v.insert_rule_version(rv(rid, GOOD_RULE, version=version))
            m["rule"].append(rid)
        elif kind == "audit":
            v.append_audit(_audit(len(m["audit"]) + 1, {"t": tenant}))
            m["audit"].append(tenant)
        else:
            n = len(m["decision"]) + 1
            v.insert_decision(f"dec-{n:05d}", rid, 1, f'"{tenant}"')
            m["decision"].append(rid)
    for tenant in ("bank-a", "bank-b", "default"):
        v = s.for_tenant(tenant)
        m = model.get(tenant, {"rule": [], "audit": [], "decision": []})
        assert sorted(r.rule_id for r in v.list_rule_versions()) == sorted(m["rule"])
        assert [e.payload["t"] for e in v.list_audit()] == m["audit"]
        assert v.list_decisions() == [f'"{tenant}"'] * len(m["decision"])
        assert v.count_decisions() == len(m["decision"])


def test_audit_chains_are_per_tenant() -> None:
    db = SQLiteStorage(":memory:")
    cfg = GateConfig(n_cases=1_000, bootstrap_resamples=100, canary_n_cases=1_000)
    a = AgentGate(db.for_tenant("bank-a"), SteppingClock(), cfg)
    b = AgentGate(db.for_tenant("bank-b"), SteppingClock(), cfg)
    a.bootstrap_baseline()
    a.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    b.bootstrap_baseline()
    assert a.audit.entries()[0].seq == b.audit.entries()[0].seq == 1
    assert len(a.audit.entries()) == len(b.audit.entries()) + 1
    assert a.verify_integrity().ok and b.verify_integrity().ok
    # tampering in one tenant is found there and nowhere else
    db.unsafe_execute("DROP TRIGGER audit_no_update")
    db.unsafe_execute("UPDATE audit_events SET actor='mallory' WHERE tenant_id='bank-a' AND seq=1")
    assert not a.verify_integrity().ok
    assert b.verify_integrity().ok


# --- upgrading a SQLite file written before tenancy -----------------------------------

LEGACY_SCHEMA = """
CREATE TABLE rule_versions (
    rule_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL, owner TEXT NOT NULL, provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL, rollback_to INTEGER,
    PRIMARY KEY (rule_id, version)
);
CREATE TRIGGER rule_versions_no_delete BEFORE DELETE ON rule_versions
BEGIN SELECT RAISE(ABORT, 'rule history is immutable'); END;
CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY, at TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL,
    payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
    body_json TEXT NOT NULL
);
"""


def test_a_pre_tenancy_file_is_upgraded_in_place(tmp_path: Path) -> None:
    # A real history, written by the current code to memory...
    src = SQLiteStorage(":memory:")
    g = AgentGate(src, SteppingClock(), GateConfig(n_cases=1_000, bootstrap_resamples=100))
    g.bootstrap_baseline()
    g.rollback(g.registry.active_rules()[0].rule_id, "model-risk@demo.bank", "drill")
    head = g.audit.head()
    # ...copied row for row into a file with the old (tenant-less) schema.
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    for table in ("rule_versions", "audit_events", "decisions"):
        rows = src._conn.query(f"SELECT * FROM {table}")
        for r in rows:
            cols = [c for c in r if c != "tenant_id"]
            con.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [r[c] for c in cols],
            )
    con.commit()
    con.close()

    up = SQLiteStorage(str(path))
    cols = [r["name"] for r in up._conn.query("PRAGMA table_info(audit_events)")]
    assert cols[-1] == "tenant_id"
    g2 = AgentGate(up, SteppingClock())
    assert g2.audit.head() == head
    assert g2.verify_integrity().ok
    assert [d.decision.decision_id for d in g2.decisions()] == ["dec-00001"]
    assert up.for_tenant("bank-a").list_audit() == []
    with pytest.raises(ImmutableHistoryError):
        up.unsafe_execute("UPDATE audit_events SET actor='mallory'")
    with pytest.raises(ImmutableHistoryError):
        up.unsafe_execute("DELETE FROM decisions")
    up.close()
    SQLiteStorage(str(path)).close()  # opening again is a no-op
