"""Liveness (/health) and readiness (/ready): what each checks, that both are
public, that the audit check is cached, and that failures name the check without
leaking driver errors. Postgres cases check the migration head."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.api.health import AuditChainCheck
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.storage import SQLiteStorage
from scruple.storage import migrate as m


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _gate(storage: SQLiteStorage | None = None) -> AgentGate:
    g = AgentGate(storage or SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=500))
    g.bootstrap_baseline()
    return g


def _client(g: AgentGate, ttl: float = 30) -> TestClient:
    return TestClient(create_app(g, settings=Settings(ready_audit_ttl_seconds=ttl)))


def _tamper(g: AgentGate) -> None:
    s = g.storage
    assert isinstance(s, SQLiteStorage)
    s.unsafe_execute("DROP TRIGGER audit_no_update")
    s.unsafe_execute("UPDATE audit_events SET actor='mallory' WHERE seq=1")


def test_health_is_public_and_does_no_io() -> None:
    g = _gate()
    client = _client(g)
    g.storage.close()  # a dead database must not fail liveness
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_on_sqlite_names_every_check() -> None:
    g = _gate()
    r = _client(g).get("/ready")  # no X-Actor, no credentials
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"ok": True, "detail": "reachable"}
    assert body["checks"]["migrations"] == {"ok": True, "detail": "not applicable (sqlite)"}
    n = len(g.audit.entries())
    assert body["checks"]["audit_chain"] == {"ok": True, "detail": f"{n} entries verified"}


def test_ready_fails_when_the_database_is_gone_without_leaking_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g = _gate()

    def broken() -> None:
        raise RuntimeError("could not connect to db-7.secret-host.internal:5432")

    monkeypatch.setattr(g.storage, "last_audit", broken)
    r = _client(g).get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": {"ok": False, "detail": "database unreachable"}}
    assert "secret-host" not in r.text


def test_ready_fails_on_a_tampered_audit_chain() -> None:
    g = _gate()
    _tamper(g)
    r = _client(g, ttl=0).get("/ready")
    assert r.status_code == 503
    check = r.json()["checks"]["audit_chain"]
    assert check == {"ok": False, "detail": "broken at position 1"}


def test_audit_check_is_cached_for_the_ttl() -> None:
    g = _gate()
    clock = FakeClock()
    check = AuditChainCheck(g, ttl=30, clock=clock)
    assert check().ok
    _tamper(g)
    clock.now = 29.9
    assert check().ok  # cached
    clock.now = 30.0
    assert not check().ok  # re-verified
    clock.now = 30.5
    assert not check().ok


def test_audit_check_with_zero_ttl_verifies_every_time() -> None:
    g = _gate()
    check = AuditChainCheck(g, ttl=0, clock=FakeClock())
    assert check().ok
    _tamper(g)
    assert not check().ok


def test_unreadable_audit_chain_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    g = _gate()

    def broken() -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(g.audit, "verify", broken)
    assert AuditChainCheck(g, ttl=0)().detail == "audit chain unreadable"


def test_ready_verifies_every_tenants_chain_from_its_own_genesis() -> None:
    g = _gate()
    other = AgentGate(g.storage.for_tenant("bank-b"), SteppingClock(), GateConfig(n_cases=500))
    other.bootstrap_baseline()
    client = _client(g, ttl=0)
    n = len(g.audit.entries()) + len(other.audit.entries())
    check = client.get("/ready").json()["checks"]["audit_chain"]
    assert check == {"ok": True, "detail": f"{n} entries verified across 2 tenant chains"}
    # Copy the default chain into a third tenant: it does not start from that
    # tenant's genesis, so readiness fails, and the answer names no tenant.
    victim = g.storage.for_tenant("bank-c")
    for e in g.audit.entries():
        victim.append_audit(e)
    r = client.get("/ready")
    assert r.status_code == 503
    detail = r.json()["checks"]["audit_chain"]["detail"]
    assert detail == "broken at position 1 (1 of 3 tenant chains)"
    assert "bank-" not in r.text


@pytest.mark.parametrize("ttl", ["-1", "86401"])
def test_ttl_is_bounded(ttl: str) -> None:
    from scruple.settings import SettingsError, load_settings

    with pytest.raises(SettingsError):
        load_settings(env={"SCRUPLE_READY_AUDIT_TTL_SECONDS": ttl})


def test_version_stays_public() -> None:
    r = _client(_gate()).get("/version")
    assert r.status_code == 200 and r.json()["name"] == "scruple"


# --- postgres -----------------------------------------------------------------


def _with_db(url: str, db: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


@pytest.fixture
def empty_db(postgres_url: str) -> Iterator[str]:
    name = f"ready_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _with_db(postgres_url, name)
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _ready(url: str) -> tuple[int, dict[str, dict[str, object]]]:
    g = AgentGate.open(url, SteppingClock(), GateConfig(n_cases=500))
    try:
        g.bootstrap_baseline()
        r = _client(g).get("/ready")
        return r.status_code, r.json()["checks"]
    finally:
        g.storage.close()


@pytest.mark.postgres
def test_postgres_without_migrations_is_not_ready(empty_db: str) -> None:
    status, checks = _ready(empty_db)  # tables created by code, alembic never ran
    assert status == 503
    assert checks["database"]["ok"] is True
    assert checks["migrations"] == {"ok": False, "detail": "not migrated (no alembic_version)"}


@pytest.mark.postgres
def test_postgres_at_head_is_ready(empty_db: str) -> None:
    m.upgrade(empty_db)
    status, checks = _ready(empty_db)
    assert status == 200, checks
    assert checks["migrations"] == {"ok": True, "detail": f"at head {m.head()}"}
    assert checks["audit_chain"]["ok"] is True


@pytest.mark.postgres
def test_postgres_behind_head_is_not_ready(empty_db: str) -> None:
    m.upgrade(empty_db)
    with psycopg.connect(empty_db, autocommit=True) as c:
        c.execute("UPDATE alembic_version SET version_num = '0000_old'")
    status, checks = _ready(empty_db)
    assert status == 503
    assert checks["migrations"] == {"ok": False, "detail": f"at 0000_old, head is {m.head()}"}
