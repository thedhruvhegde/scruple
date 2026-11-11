"""Usage events and saved console filters on every backend: idempotent keys, tenant
scoping, append-only enforcement by the database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scruple.storage import ImmutableHistoryError
from scruple.storage.console_filters import SQLSavedFilters, filter_id
from scruple.usage.meter import Meter, UsageEvent, UsageStore

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


def _ev(key: str, tenant: str = "acme", q: int = 1, at: datetime = T0) -> UsageEvent:
    return UsageEvent(
        tenant=tenant, event_key=key, meter=Meter.GATE_RUN, quantity=q, at=at, source=key
    )


def test_usage_events(store: Any) -> None:
    u = UsageStore(store.connection)
    assert u.record_many([_ev("a"), _ev("b", at=T0 + timedelta(days=31)), _ev("a", q=7)]) == 2
    assert u.record(_ev("a", tenant="beta"))
    assert [e.event_key for e in u.events("acme")] == ["a", "b"]
    assert [a.quantity for a in u.aggregate("acme", "month")] == [1, 1]
    assert u.keys("beta") == {"a"}
    with pytest.raises(ImmutableHistoryError), store.transaction():
        store.connection.execute("UPDATE usage_events SET quantity = 5")
    with pytest.raises(ImmutableHistoryError), store.transaction():
        store.connection.execute("DELETE FROM usage_events")


def test_saved_filters(store: Any) -> None:
    f = SQLSavedFilters(store.connection)
    f.save("acme", "runs", "x", "q=a", "alice", T0)
    f.save("acme", "runs", "x", "q=b", "alice", T0)
    assert [(s.name, s.query) for s in f.list("acme", "runs")] == [("x", "q=b")]
    assert f.list("beta", "runs") == []
    f.delete("acme", filter_id("runs", "x"), "alice", T0)
    assert f.list("acme", "runs") == []
    with pytest.raises(ImmutableHistoryError), store.transaction():
        store.connection.execute("DELETE FROM console_filter_events")
