"""Tenant directory over each backend: tenants found from their rows, audited
create and quota, append-only events (`scruple.tenancy.directory`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import key_permissions
from scruple.service import AgentGate
from scruple.storage.retention import tenant_gate
from scruple.tenancy.directory import (
    EVENT_CREATED,
    EVENT_QUOTA,
    Quota,
    TenancyError,
    TenantDirectory,
)
from tests.storage.conftest import RawStorage
from tests.storage.test_retention import BATCH, _shadow
from tests.storage.test_retention import Clock as RetentionClock

A, B = "bank-a", "bank-b"
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


class Clock(RetentionClock):
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        self.t += timedelta(milliseconds=1)
        return self.t


def key(store: RawStorage, clock: Clock, tenant: str) -> None:
    svc = ApiKeyService(store, clock, ScryptParams(n=1024), permissions_for=key_permissions)
    svc.create("u", (), "local-admin", tenant=tenant, roles=["auditor"])


def test_tenants_are_found_wherever_they_left_rows(store: RawStorage) -> None:
    clock = Clock()
    d = TenantDirectory(store, clock)
    assert d.list() == []
    key(store, clock, A)
    _shadow(store, clock, B).record(BATCH[:3])  # imported shadow rows only
    AgentGate(store.for_tenant("c-bank"), clock).bootstrap_baseline()
    assert d.known() == [A, B, "c-bank"]
    [a, _, c] = d.list()
    assert (a.tenant, a.status, a.explicit) == (A, "active", False)
    assert c.quota == Quota()


def test_create_quota_are_audited_in_the_tenants_chain(store: RawStorage) -> None:
    clock = Clock()
    gate = AgentGate(store, clock)
    d = TenantDirectory(store, clock)
    rec = d.create(A, "ops")
    assert rec.explicit and rec.created_by == "ops" and rec.status == "active"
    with pytest.raises(TenancyError, match="already exists"):
        d.create(A, "ops")
    with pytest.raises(TenancyError, match="create it first"):
        d.set_quota(B, Quota(gate_runs_per_day=3), "ops")
    assert d.set_quota(A, Quota(gate_runs_per_day=3), "ops").quota.gate_runs_per_day == 3
    assert d.set_quota(A, Quota(), "ops").quota.gate_runs_per_day is None
    ta = tenant_gate(gate, A)
    assert [e.event_type for e in ta.audit.entries()] == [EVENT_CREATED, EVENT_QUOTA, EVENT_QUOTA]
    assert ta.audit.verify().ok and ta.verify_integrity().ok
    assert gate.audit.entries() == []  # nothing in the operator's (default) chain
    with pytest.raises(TenancyError, match="invalid tenant"):
        d.create("bad tenant!", "ops")


def test_tenant_events_are_append_only(store: RawStorage) -> None:
    d = TenantDirectory(store, Clock())
    d.create(A, "ops")
    for sql in ("UPDATE tenant_events SET actor = 'mallory'", "DELETE FROM tenant_events"):
        with pytest.raises(Exception, match="append-only"):
            store.unsafe_execute(sql)
    assert d.get(A).created_by == "ops"
