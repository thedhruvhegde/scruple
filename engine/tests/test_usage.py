"""Usage metering (D08): append-only events per tenant from gate runs and rollouts,
idempotent sync, aggregation per period, the CLI report and the admin-only API."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import key_permissions
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from scruple.usage.meter import (
    Meter,
    Period,
    UsageEvent,
    UsageStore,
    events_from_decisions,
    next_period,
    period_start,
)
from tests.ci_support import PROV

CFG = GateConfig(
    n_cases=800,
    bootstrap_resamples=100,
    canary_n_cases=2_000,
    eval_seed=918_273_645,
    canary_seed=827_364_519,
)
T0 = datetime(2026, 1, 31, 23, 0, tzinfo=UTC)


def _gate(db: SQLiteStorage, tenant: str = "default") -> AgentGate:
    g = AgentGate(db.for_tenant(tenant), SteppingClock(), CFG)
    g.bootstrap_baseline()
    return g


def _ev(
    key: str, at: datetime, meter: Meter = Meter.GATE_RUN, q: int = 1, tenant: str = "acme"
) -> UsageEvent:
    return UsageEvent(tenant=tenant, event_key=key, meter=meter, quantity=q, at=at, source=key)


@pytest.fixture
def evaluated() -> tuple[SQLiteStorage, AgentGate]:
    db = SQLiteStorage(":memory:")
    g = _gate(db)
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.approve(GOOD_RULE_ID, 1, "model-risk@demo.bank")
    return db, g


def test_events_follow_the_decisions(evaluated: tuple[SQLiteStorage, AgentGate]) -> None:
    _, g = evaluated
    stages = [r.decision.stage.value for r in g.decisions()]
    assert stages == ["shadow", "canary", "activation"]
    evs = list(events_from_decisions("default", g.decisions()))
    by_meter = {m: [e for e in evs if e.meter is m] for m in Meter}
    assert len(by_meter[Meter.GATE_RUN]) == 2
    assert len(by_meter[Meter.ROLLOUT]) == 1
    shadow, canary = (r.decision for r in g.decisions()[:2])
    assert shadow.shadow is not None and canary.canary is not None
    assert [e.quantity for e in by_meter[Meter.GATE_CASES]] == [
        shadow.shadow.n_cases,
        canary.canary.live_cases,
    ]
    assert all(
        e.at
        == next(r.decision.created_at for r in g.decisions() if r.decision.decision_id == e.source)
        for e in evs
    )


def test_sync_is_idempotent(evaluated: tuple[SQLiteStorage, AgentGate]) -> None:
    db, g = evaluated
    store = UsageStore(db.connection)
    first = store.sync(g)
    assert first == 5
    assert store.sync(g) == 0
    g.run_assurance()
    # One gate run. An assurance decision records no case count, so no cases event.
    assert store.sync(g) == 1
    totals = {a.meter: a.quantity for a in store.aggregate("default", "month")}
    assert totals[Meter.GATE_RUN] == 3 and totals[Meter.ROLLOUT] == 1


def test_a_blocked_or_rolled_back_rule_counts_its_runs_but_no_rollout() -> None:
    from scruple.sim.library import BAD_RULE, BAD_RULE_ID

    db = SQLiteStorage(":memory:")
    g = _gate(db)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(BAD_RULE_ID, 1)
    base = g.registry.active_rules()[0].rule_id
    g.rollback(base, "model-risk@demo.bank", "test rollback")
    store = UsageStore(db.connection)
    store.sync(g)
    meters = {a.meter for a in store.aggregate("default")}
    assert Meter.ROLLOUT not in meters and Meter.GATE_RUN in meters


def test_events_are_per_tenant_and_append_only() -> None:
    db = SQLiteStorage(":memory:")
    store = UsageStore(db.connection)
    assert store.record(_ev("k1", T0, tenant="acme"))
    assert not store.record(_ev("k1", T0, tenant="acme", q=99))  # same key: no-op
    assert store.record(_ev("k1", T0, tenant="beta"))  # another tenant's key space
    assert [e.quantity for e in store.events("acme")] == [1]
    assert len(store.events("beta")) == 1 and store.events("gamma") == []
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("UPDATE usage_events SET quantity = 0")
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("DELETE FROM usage_events")


def test_aggregation_by_day_and_month_in_utc() -> None:
    db = SQLiteStorage(":memory:")
    store = UsageStore(db.connection)
    store.record_many(
        [
            _ev("a", T0),  # Jan 31 23:00 UTC
            _ev("b", T0 + timedelta(hours=2)),  # Feb 1 01:00 UTC
            _ev("c", T0 + timedelta(days=1), Meter.GATE_CASES, 500),
            _ev("d", T0 + timedelta(days=40), Meter.ROLLOUT),
        ]
    )
    months = [
        (a.period_start.isoformat(), a.meter.value, a.quantity) for a in store.aggregate("acme")
    ]
    assert months == [
        ("2026-01-01", "gate_run", 1),
        ("2026-02-01", "gate_run", 1),
        ("2026-02-01", "gate_cases", 500),
        ("2026-03-01", "rollout", 1),
    ]
    feb = store.aggregate("acme", "month", date(2026, 2, 1), date(2026, 3, 1))
    assert {a.meter for a in feb} == {Meter.GATE_RUN, Meter.GATE_CASES}
    assert all(a.period_end == date(2026, 3, 1) for a in feb)
    days = store.aggregate("acme", "day", date(2026, 2, 1), date(2026, 2, 2))
    assert [(a.meter.value, a.quantity) for a in days] == [("gate_run", 1), ("gate_cases", 500)]


@given(
    st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
    )
)
def test_every_instant_falls_in_exactly_one_period(at: datetime) -> None:
    periods: tuple[Period, ...] = ("day", "month")
    for period in periods:
        start = period_start(at, period)
        end = next_period(start, period)
        assert start <= at.date() < end
        assert period_start(end, period) == end


def test_naive_times_are_refused() -> None:
    from scruple.usage.meter import UsageError

    db = SQLiteStorage(":memory:")
    with pytest.raises(UsageError):
        UsageStore(db.connection).record(_ev("x", datetime(2026, 1, 1)))


@pytest.mark.parametrize("key", ["", "has space", "x" * 201, "<b>"])
def test_bad_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError):
        _ev(key, T0)


def test_cli_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "g.db")
    f = tmp_path / "rule.json"
    f.write_text(
        json.dumps(
            {
                "rule_id": GOOD_RULE_ID,
                "spec": GOOD_RULE.model_dump(mode="json"),
                "owner": "ops",
                "provenance": {"agent_id": "agent-1"},
            }
        )
    )
    assert main(["propose", str(f), "--db", db]) == 0
    assert main(["evaluate", GOOD_RULE_ID, "1", "--db", db]) == 0
    capsys.readouterr()
    assert main(["usage", "sync", "--db", db]) == 0
    assert "recorded" in capsys.readouterr().out
    assert main(["usage", "report", "--db", db, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["meter"] for r in rows} == {"gate_run", "gate_cases"}
    assert sum(r["quantity"] for r in rows if r["meter"] == "gate_run") == 2
    assert main(["usage", "report", "--db", db, "--period", "day"]) == 0
    out = capsys.readouterr().out
    assert "gate_run" in out and "per day" in out
    assert main(["usage", "report", "--db", db, "--tenant", "other"]) == 0
    assert "no usage recorded" in capsys.readouterr().out


def test_api_is_admin_only_and_tenant_scoped(evaluated: tuple[SQLiteStorage, AgentGate]) -> None:
    db, g = evaluated
    keys = ApiKeyService(db, params=ScryptParams(n=1024), permissions_for=key_permissions)

    def hdr(roles: list[str], tenant: str = "default") -> dict[str, str]:
        _, tok = keys.create(
            f"u-{roles[0]}-{tenant}", (), "local-admin", tenant=tenant, roles=roles
        )
        return {"X-API-Key": tok.get_secret_value()}

    c = TestClient(create_app(g, auth=Authenticator(keys)))
    assert c.get("/usage").status_code == 401
    for role in ("auditor", "approver", "proposer", "service"):
        r = c.get("/usage", headers=hdr([role]))
        assert r.status_code == 403 and "admin" in r.json()["detail"]
    admin = hdr(["risk_admin"])
    r = c.get("/usage", headers=admin)
    assert r.status_code == 200
    totals = {row["meter"]: row["quantity"] for row in r.json()}
    assert totals["gate_run"] == 2 and totals["rollout"] == 1
    r = c.get("/usage/events?limit=2", headers=admin)
    assert r.status_code == 200 and len(r.json()) == 2 and r.headers["x-total-count"] == "5"
    assert c.get("/usage?period=week", headers=admin).status_code == 422
    for bad in ("0", "1700000000", "2026-13-01", "11/11/2026"):
        assert c.get(f"/usage?start={bad}", headers=admin).status_code == 422, bad
    assert c.get("/usage?tenant=other", headers=admin).status_code == 422
    other = hdr(["risk_admin"], tenant="other-bank")
    assert c.get("/usage", headers=other).json() == []  # its own (empty) usage only
    day = g.decisions()[0].decision.created_at.date().isoformat()
    r = c.get(f"/usage?period=day&start={day}", headers=admin)
    assert r.status_code == 200 and all(row["period"] == "day" for row in r.json())
