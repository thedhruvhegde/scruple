"""Tenant administration: list, show, create, quota, offboard (`scruple.tenancy`)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticated, Authenticator, AuthError
from scruple.auth.keys import ApiKeyService, InvalidKeyError, ScryptParams
from scruple.auth.models import Permission, Principal
from scruple.auth.rbac import key_permissions
from scruple.cli.main import main
from scruple.gate import GateConfig
from scruple.ingest.models import IngestBatch
from scruple.ingest.store import CaseStore
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from scruple.storage.retention import END_OF_TIME, EVENT_PURGED, tenant_gate
from scruple.tenancy.directory import (
    EVENT_OFFBOARDED,
    Quota,
    QuotaExceededError,
    TenancyError,
    TenantDirectory,
    TenantOffboardedError,
)
from scruple.tenancy.offboard import offboard, plan_offboarding
from tests.ci_support import PROV
from tests.storage.test_retention import BATCH, ROWS, _shadow
from tests.storage.test_retention import Clock as RetentionClock

A, B = "bank-a", "bank-b"
FAST = ScryptParams(n=1024)
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
CFG = GateConfig(
    n_cases=800,
    bootstrap_resamples=100,
    canary_n_cases=2_000,
    eval_seed=918_273_645,
    canary_seed=827_364_519,
)


class Clock(RetentionClock):
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def now(self) -> datetime:
        self.t += timedelta(milliseconds=1)
        return self.t


def keys_for(storage: Any, clock: Any = None) -> ApiKeyService:
    return ApiKeyService(storage, clock, FAST, permissions_for=key_permissions)


def key(svc: ApiKeyService, tenant: str, role: str = "auditor") -> str:
    _, tok = svc.create(f"u-{role}", (), "local-admin", tenant=tenant, roles=[role])
    return tok.get_secret_value()


N_CASES = 6
N_MOVES = sum(len(mv) for _, mv in ROWS[:N_CASES])


def case_store(path: Path, tenant: str, n: int = N_CASES) -> CaseStore:
    cs = CaseStore(path)
    for i, (c, mv) in enumerate(ROWS[:n]):
        cs.write_webhook(
            source="stripe",
            event_id=f"evt_{i}",
            event_type="charge.dispute.created",
            event_at=c.opened_at,
            payload_sha256="0" * 64,
            batch=IngestBatch(cases=(c,), movements=mv),
            tenant_id=tenant,
        )
    return cs


# --- offboarding ---------------------------------------------------------------------


@pytest.fixture
def two_banks(tmp_path: Path) -> Any:
    """bank-a and bank-b each with keys, imported shadow rows and a case store."""
    clock = Clock()
    db = SQLiteStorage(str(tmp_path / "gate.db"))
    gate = AgentGate(db, clock)
    svc = keys_for(db, clock)
    tokens = {t: [key(svc, t), key(svc, t, "approver")] for t in (A, B)}
    _shadow(db, clock, A).record(BATCH[:10])
    _shadow(db, clock, B).record(BATCH[:5])
    stores = {t: case_store(tmp_path / f"{t}.db", t) for t in (A, B)}
    yield gate, svc, tokens, stores
    for cs in stores.values():
        cs.close()
    db.close()


def test_dry_run_changes_nothing(two_banks: Any, tmp_path: Path) -> None:
    gate, svc, _, stores = two_banks
    before = (tmp_path / "gate.db").read_bytes()
    plan = plan_offboarding(gate, svc, A, stores[A])
    assert len(plan.keys_to_revoke) == 2 and not plan.already_offboarded
    assert plan.gate_rows == {"imported_shadow_events": 1}
    assert plan.case_store_rows == {
        "cases": N_CASES,
        "movements": N_MOVES,
        "webhook_case_events": N_CASES,
    }
    assert (tmp_path / "gate.db").read_bytes() == before
    with pytest.raises(TenancyError, match="no tenant"):
        plan_offboarding(gate, svc, "nobody", None)


def test_offboard_revokes_purges_audits_and_leaves_others(two_banks: Any) -> None:
    gate, svc, tokens, stores = two_banks
    b_head = tenant_gate(gate, B).audit.head()
    res = offboard(gate, svc, A, "ops", stores[A])
    assert res.recorded and len(res.keys_revoked) == 2
    by_table = {(p["store"], p["table"]): p["rows"] for p in res.purges}
    assert by_table == {
        ("gate", "imported_shadow_events"): 1,
        ("case_store", "cases"): N_CASES,
        ("case_store", "movements"): N_MOVES,
        ("case_store", "webhook_case_events"): N_CASES,
    }
    # Keys no longer authenticate; new keys are refused.
    for tok in tokens[A]:
        with pytest.raises(InvalidKeyError):
            svc.authenticate(tok)
    with pytest.raises(ValueError, match="offboarded"):
        key(svc, A)
    # Data is gone; the chain records each step and still verifies.
    assert stores[A].cases() == [] and stores[A].movements() == []
    assert TenantDirectory(gate.storage).imported_shadow_rows(A) == 0
    ta = tenant_gate(gate, A)
    types = [e.event_type for e in ta.audit.entries()]
    assert types.index(EVENT_OFFBOARDED) < types.index("auth.key_revoked")
    purged = [e.payload for e in ta.audit.entries() if e.event_type == EVENT_PURGED]
    assert len(purged) == 4
    assert all(p["reason"] == "offboarding" and "days" not in p for p in purged)
    assert all(p["cutoff"] == END_OF_TIME.isoformat() for p in purged)
    assert ta.audit.verify().ok and ta.verify_integrity().ok
    assert res.audit_head_seq == ta.audit.entries()[-1].seq
    # bank-b is untouched.
    assert tenant_gate(gate, B).audit.head() == b_head
    assert all(svc.authenticate(tok) for tok in tokens[B])
    assert len(stores[B].cases()) == N_CASES
    assert TenantDirectory(gate.storage).imported_shadow_rows(B) == 1
    assert TenantDirectory(gate.storage).get(A).status == "offboarded"
    with pytest.raises(TenantOffboardedError):
        TenantDirectory(gate.storage).create(A, "ops")


def test_offboarding_again_finishes_and_records_nothing_new(two_banks: Any) -> None:
    gate, svc, _, stores = two_banks
    offboard(gate, svc, A, "ops", None)  # first run without the case store
    n = len(tenant_gate(gate, A).audit.entries())
    again = offboard(gate, svc, A, "ops", stores[A])  # finish with it
    assert not again.recorded and again.keys_revoked == ()
    assert {p["table"] for p in again.purges} == {"cases", "movements", "webhook_case_events"}
    entries = tenant_gate(gate, A).audit.entries()
    assert len(entries) == n + 3 and EVENT_OFFBOARDED not in [e.event_type for e in entries[n:]]
    third = offboard(gate, svc, A, "ops", stores[A])
    assert third.purges == () and len(tenant_gate(gate, A).audit.entries()) == n + 3


def test_another_tenants_case_store_is_refused(two_banks: Any) -> None:
    gate, svc, tokens, stores = two_banks
    with pytest.raises(TenancyError, match="one tenant's file"):
        offboard(gate, svc, A, "ops", stores[B])
    assert all(svc.authenticate(tok) for tok in tokens[A])  # nothing happened
    assert not TenantDirectory(gate.storage).is_offboarded(A)


class Tokens:
    """A bearer-token verifier that trusts the token text as `subject@tenant`."""

    def verify(self, token: str) -> Principal:
        subject, _, tenant = token.partition("@")
        return Principal(
            subject=subject,
            tenant=tenant,
            permissions=frozenset({Permission.READ}),
            roles=(),
            method="oidc",
            credential_id=token,
        )


def test_tokens_naming_an_offboarded_tenant_are_refused(two_banks: Any) -> None:
    gate, svc, _, _ = two_banks
    d = TenantDirectory(gate.storage)
    auth = Authenticator(svc, Tokens(), tenant_refused=d.is_offboarded)
    hdr = {"authorization": "Bearer jo@bank-a"}
    who = auth.authenticate(hdr)
    assert isinstance(who, Authenticated) and who.principal.tenant == A
    offboard(gate, svc, A, "ops")
    with pytest.raises(AuthError, match="invalid bearer token"):
        auth.authenticate(hdr)
    assert auth.authenticate({"authorization": "Bearer jo@bank-b"}) is not None


def test_offboarded_tenant_gets_401_over_http(two_banks: Any) -> None:
    gate, svc, tokens, _ = two_banks
    d = TenantDirectory(gate.storage)
    c = TestClient(create_app(gate, auth=Authenticator(svc, tenant_refused=d.is_offboarded)))
    assert c.get("/auth/whoami", headers={"X-API-Key": tokens[A][0]}).status_code == 200
    offboard(gate, svc, A, "ops")
    r = c.get("/auth/whoami", headers={"X-API-Key": tokens[A][0]})
    assert r.status_code == 401 and r.json()["detail"] == "invalid API key"
    assert c.get("/auth/whoami", headers={"X-API-Key": tokens[B][0]}).status_code == 200


# --- quota ---------------------------------------------------------------------------


def test_daily_gate_run_quota() -> None:
    clock = Clock()
    db = SQLiteStorage(":memory:")
    g = AgentGate(db.for_tenant(A), clock, CFG)
    g.bootstrap_baseline()
    d = TenantDirectory(db, clock)
    d.create(A, "ops")
    d.set_quota(A, Quota(gate_runs_per_day=2), "ops")
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    assert len(g.evaluate(GOOD_RULE_ID, 1)) == 2  # shadow and canary: two gate runs
    n = len(g.decisions())
    with pytest.raises(QuotaExceededError, match="quota of 2") as exc:
        g.evaluate(BAD_RULE_ID, 1)
    midnight = datetime(2026, 3, 3, tzinfo=UTC)
    assert 0 < exc.value.retry_after <= (midnight - T0).total_seconds() + 1
    assert len(g.decisions()) == n and g.registry.get(BAD_RULE_ID, 1).status.value == "candidate"
    assert g.run_assurance() is not None  # guards what is live: never refused
    clock.t = midnight + timedelta(hours=1)  # the next UTC day
    assert g.evaluate(BAD_RULE_ID, 1)
    # Another tenant has no quota.
    other = AgentGate(db.for_tenant(B), clock, CFG)
    other.bootstrap_baseline()
    other.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    assert other.evaluate(GOOD_RULE_ID, 1)
    db.close()


def test_quota_answers_429_and_get_tenant_is_admin_only() -> None:
    clock = Clock()
    db = SQLiteStorage(":memory:")
    gate = AgentGate(db, clock, CFG)
    gate.bootstrap_baseline()
    svc = keys_for(db, clock)
    d = TenantDirectory(db, clock)
    d.create(A, "ops")
    d.set_quota(A, Quota(gate_runs_per_day=1), "ops")
    c = TestClient(create_app(gate, auth=Authenticator(svc, tenant_refused=d.is_offboarded)))
    admin = {"X-API-Key": key(svc, A, "risk_admin")}
    agent = {"X-API-Key": key(svc, A, "proposer")}
    auditor = {"X-API-Key": key(svc, A, "auditor")}
    assert c.get("/tenant").status_code == 401
    assert c.get("/tenant", headers=auditor).status_code == 403
    r = c.get("/tenant", headers=admin)
    assert r.status_code == 200
    assert r.json()["tenant"]["quota"] == {"gate_runs_per_day": 1}
    assert r.json()["gate_runs_today"] == 0
    tg = tenant_gate(gate, A)
    tg.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    tg.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    assert c.post(f"/rules/{GOOD_RULE_ID}/versions/1/evaluate", headers=agent).status_code == 200
    r = c.post(f"/rules/{BAD_RULE_ID}/versions/1/evaluate", headers=agent)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "quota" in r.json()["detail"]
    assert c.get("/tenant", headers=admin).json()["gate_runs_today"] == 2
    db.close()


# --- command line --------------------------------------------------------------------


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "gate.db")
    cs_path = tmp_path / "a-cases.db"
    case_store(cs_path, A).close()
    assert run(capsys, "keys", "create", "--db", db, "--tenant", A, "--subject", "x",
               "--role", "auditor")[0] == 0  # fmt: skip
    code, out, _ = run(capsys, "tenant", "list", "--db", db)
    assert code == 0 and out.split()[:3] == [A, "active", "implicit"]
    assert run(capsys, "tenant", "create", "--db", db, A, "--actor", "ops")[0] == 0
    code, _, err = run(capsys, "tenant", "quota", "--db", db, A)
    assert code == 2 and "--gate-runs-per-day" in err
    code, out, _ = run(capsys, "tenant", "quota", "--db", db, A, "--gate-runs-per-day", "40")
    assert code == 0 and json.loads(out)["quota"] == {"gate_runs_per_day": 40}
    code, out, _ = run(capsys, "tenant", "show", "--db", db, A)
    shown = json.loads(out)
    assert shown["keys_active"] == 1 and shown["explicit"] and shown["audit_entries"] == 3
    assert run(capsys, "tenant", "show", "--db", db, "nobody")[0] == 2

    before = Path(db).read_bytes(), cs_path.read_bytes()
    code, out, err = run(capsys, "tenant", "offboard", "--db", db, A, "--case-store", str(cs_path))
    plan = json.loads(out)
    assert code == 0 and plan["dry_run"] and len(plan["keys_to_revoke"]) == 1
    assert plan["case_store_rows"]["cases"] == N_CASES and "--yes" in err
    assert (Path(db).read_bytes(), cs_path.read_bytes()) == before

    code, out, err = run(
        capsys, "tenant", "offboard", "--db", db, A, "--case-store", str(cs_path), "--yes"
    )
    done = json.loads(out)
    assert code == 0 and not done["dry_run"] and len(done["keys_revoked"]) == 1
    assert f"1 keys revoked, {2 * N_CASES + N_MOVES} rows purged in 3 tables" in err
    code, out, _ = run(capsys, "tenant", "list", "--db", db, "--json")
    assert json.loads(out)[0]["status"] == "offboarded"
    code, _, err = run(capsys, "keys", "create", "--db", db, "--tenant", A, "--subject", "y",
                       "--role", "auditor")  # fmt: skip
    assert code == 2 and "offboarded" in err
    assert run(capsys, "tenant", "create", "--db", db, A)[0] == 2
    con = sqlite3.connect(cs_path)
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    con.close()
