"""Policy and integration tables on every backend: append-only history is enforced by
the database, the mutable state tables stay mutable, and SQLite still creates them
only on first use."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scruple.integrations.config import IntegrationSettings
from scruple.integrations.store import IntegrationStore
from scruple.policy.models import Policy
from scruple.policy.store import PolicyStore
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from scruple.storage import integrations as integration_tables
from scruple.storage.lazy import table_exists

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


def _refused(store: Any, sql: str) -> None:
    with pytest.raises(ImmutableHistoryError), store.transaction():
        store.connection.execute(sql)


def test_policy_history_is_append_only(store: Any) -> None:
    p = PolicyStore(store)
    rec = p.append(Policy(name="acme-disputes"), "alice", T0)
    assert rec.number == 1 and p.current() == rec
    _refused(store, "UPDATE tenant_policies SET actor = 'mallory'")
    _refused(store, "DELETE FROM tenant_policies")
    assert [r.actor for r in p.history()] == ["alice"]


@pytest.mark.parametrize("table", integration_tables.APPEND_ONLY)
def test_integration_history_tables_are_append_only(store: Any, table: str) -> None:
    s = IntegrationStore(store, tenant="acme")
    s.put_settings(IntegrationSettings(), "alice", T0)
    s.consume_nonce("n-1", "approve", T0)
    s.mark_ticket_updated("jira", "dec-1", "r-1@1", T0)
    with store.transaction():  # the event and attempt rows, as the dispatcher writes them
        store.connection.execute(
            "INSERT INTO integration_events (tenant_id, event_id, audit_seq, event_type, "
            "body_json, created_at) VALUES ('acme', 'evt-1', 1, 'decision.recorded', '{}', ?)",
            (T0.isoformat(),),
        )
        store.connection.execute(
            "INSERT INTO webhook_attempts (tenant_id, delivery_id, attempt_no, at, "
            "status_code, error, duration_ms, job_id) "
            "VALUES ('acme', 'whd-1', 1, ?, 200, NULL, 5, 'job-1')",
            (T0.isoformat(),),
        )
    _refused(store, f"UPDATE {table} SET tenant_id = 'mallory'")
    _refused(store, f"DELETE FROM {table}")


def test_integration_state_tables_stay_mutable(store: Any) -> None:
    s = IntegrationStore(store, tenant="acme")
    s.set_cursor(3)
    s.set_cursor(7)
    assert s.cursor() == 7
    assert s.claim_ticket("jira", "r-1@1", T0) and not s.claim_ticket("jira", "r-1@1", T0)
    s.release_ticket("jira", "r-1@1")
    assert s.ticket("jira", "r-1@1") is None
    assert s.claim_ticket("jira", "r-1@1", T0)
    s.complete_ticket("jira", "r-1@1", "10001", "GATE-1", "https://jira.invalid/GATE-1")
    ticket = s.ticket("jira", "r-1@1")
    assert ticket is not None and ticket["issue_key"] == "GATE-1"


def test_sqlite_creates_the_tables_on_first_use_only() -> None:
    s = SQLiteStorage(":memory:")
    names = ("tenant_policies", *integration_tables.TABLES)
    assert not any(table_exists(s.connection, "sqlite", t) for t in names)
    assert IntegrationStore.tenants(s) == [] and PolicyStore(s).current() is None
    assert not any(table_exists(s.connection, "sqlite", t) for t in names)
    IntegrationStore(s).ensure()
    PolicyStore(s).append(Policy(name="acme-disputes"), "alice", T0)
    assert all(table_exists(s.connection, "sqlite", t) for t in names)
    s.close()
