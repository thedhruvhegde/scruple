"""Cross-tenant access over HTTP is impossible: every route, every role, generated data.

Tenant A gets generated candidate rules, decisions and shadow state. Every route is
then called with tenant B's credentials (one key per role) on A's identifiers. Each
answer is 403 or 404, or, for list and verification routes, B's own data with no
trace of A's. A's history is unchanged afterwards.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.api.strict import takes_body
from scruple.audit.models import genesis_hash
from scruple.auth.authn import Authenticator
from scruple.auth.deps import iter_routes, protection_of
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import Limit
from scruple.auth.rbac import Role, key_permissions
from scruple.clock import SteppingClock
from scruple.domain import Action, RuleSpec
from scruple.gate import GateConfig
from scruple.gate.shadow import LiveDecision
from scruple.jobs import JobKind, JobSubmit
from scruple.jobs.queue import queue_for
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import GOOD_RULE
from scruple.storage import SQLiteStorage
from tests.test_gate_shadow_imported import decisions as imported_decisions
from tests.test_gate_shadow_imported import synthetic

FAST = ScryptParams(n=1024)
A, B = "bank-a", "bank-b"
LISTS = {
    "/rules",
    "/decisions",
    "/audit/verify",
    "/auth/whoami",
    "/auth/keys",
    "/openapi.json",
    "/jobs",
    "/usage",
    "/usage/events",
    # console pages that list the caller's own tenant (or the shared synthetic runs)
    "/console/",
    "/console/rules",
    "/console/dashboard",
    "/console/dashboard/savings",
    "/console/dashboard/payments",
    "/console/dashboard/routing",
    "/console/dashboard/compute",
    "/console/runs",
    "/console/approvals",
    "/console/diff/rules",
    "/console/diff/agents",
    "/console/backtests",
    "/policy/history",
    "/integrations/settings",
    "/integrations/webhooks",
    "/integrations/webhooks/deliveries",
    "/integrations/tickets",
}
# Routes that create in the caller's own tenant.
CREATES = {
    "/rules",
    "/jobs",
    "/policy",
    "/integrations/settings",
    "/integrations/webhooks",
    "/integrations/dispatch",
}
CFG = GateConfig(n_cases=1_000, bootstrap_resamples=100, canary_n_cases=1_000)


class World:
    def __init__(self) -> None:
        self.db = SQLiteStorage(":memory:")
        root = AgentGate(self.db, SteppingClock(), CFG)
        root.bootstrap_baseline()
        self.keys = ApiKeyService(self.db, params=FAST, permissions_for=key_permissions)
        # Rate limits out of the way: this test sends hundreds of requests per key.
        many = Limit(per_minute=1_000_000, burst=1_000_000)
        limits = Limits(default=many, anonymous=many, auth_failures=many)
        self.client = TestClient(create_app(root, auth=Authenticator(self.keys), limits=limits))
        self.queue = queue_for(self.db)
        self.headers: dict[str, dict[str, dict[str, str]]] = {}
        for tenant in (A, B):
            for role in Role:
                _, tok = self.keys.create(
                    f"{role.value}-{tenant}", [], "local-admin", tenant=tenant, roles=[role]
                )
                self.headers.setdefault(tenant, {})[role.value] = {
                    "X-API-Key": tok.get_secret_value()
                }
        # the named approver of each tenant
        for tenant in (A, B):
            _, tok = self.keys.create(
                "model-risk@demo.bank", [], "local-admin", tenant=tenant, roles=[Role.APPROVER]
            )
            self.headers[tenant]["named"] = {"X-API-Key": tok.get_secret_value()}

    def gate(self, tenant: str) -> AgentGate:
        return AgentGate(self.db.for_tenant(tenant), SteppingClock(), CFG)


@pytest.fixture(scope="module")
def world() -> Iterator[World]:
    w = World()
    yield w
    w.db.close()


def _routes(w: World) -> list[Any]:
    return list(iter_routes(cast(FastAPI, w.client.app)))


def _fill(path: str, rule_id: str, decision_id: str, job_id: str = "job-00000001") -> str:
    values = {
        "rule_id": rule_id,
        "version": "1",
        "decision_id": decision_id,
        "job_id": job_id,
        "name": "x",
        "episode_id": "ep-000000000000",
        "filter_id": "flt-000000000000",
        "endpoint_id": "whe_0000000000000000",
        "delivery_id": "whd_000000000000000000000000",
    }
    return re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], path)


def _bodies(agent: str) -> dict[str, dict[str, Any]]:
    """Valid bodies, so a probe gets past validation to the tenant lookup."""
    live = [
        LiveDecision(case=c, action=Action.CONTEST, agent_id=agent).model_dump(mode="json")
        for c in generate_cases(3, 2, prefix="t")
    ]
    rows = synthetic(2)
    imported = [
        json.loads(d.model_copy(update={"agent_id": agent}).model_dump_json())
        for d in imported_decisions(rows, as_opened=True)
    ]
    return {
        "/rules/{rule_id}/rollback": {"reason": "cross-tenant probe"},
        # B's own (made-up) rule: a job is created in B's tenant and names nothing of A's
        "/jobs": {"kind": "gate_evaluation", "params": {"rule_id": "b-probe", "version": 1}},
        "/shadow/rules/{rule_id}/versions/{version}/decisions": {"decisions": live},
        "/shadow/rules/{rule_id}/versions/{version}/imported/decisions": {"decisions": imported},
        "/shadow/rules/{rule_id}/versions/{version}/imported/outcomes": {
            "outcomes": [{"case": json.loads(c.model_dump_json())} for c, _ in rows]
        },
    }


def _body(route: Any, agent: str) -> dict[str, Any] | None:
    if "POST" not in (route.methods or set()) or not takes_body(route):
        return None
    return _bodies(agent).get(route.path, {})


rule_ids = st.lists(
    st.from_regex(r"a-[a-z0-9]{3,12}", fullmatch=True), min_size=1, max_size=3, unique=True
)


@settings(max_examples=8, deadline=None, suppress_health_check=list(HealthCheck))
@given(rule_ids, st.integers(1_000, 900_000))
def test_no_route_reaches_another_tenants_data(
    world: World, ids: list[str], threshold: int
) -> None:
    w = world
    a = w.gate(A)
    a.bootstrap_baseline()
    prov = Provenance(agent_id="proposer-bank-a")
    created = []
    for i, rid in enumerate(ids):
        if a.registry.versions(rid):
            continue
        spec = RuleSpec.model_validate(
            {
                **GOOD_RULE.model_dump(mode="json"),
                "conditions": [
                    {"field": "amount_usd_minor", "op": "gt", "value": threshold + i * 7}
                ],
            }
        )
        try:
            created.append(a.propose(rid, spec, "ops-team-a", prov).rule_id)
        except Exception:
            continue
    # a decision and a rollback in A, so A has decisions and an audit trail
    base = a.registry.active_rules()
    if base:
        a.rollback(base[0].rule_id, "model-risk@demo.bank", "seed a decision")
    a_decisions = [d.decision for d in a.decisions()]
    a_head = a.audit.head()
    target_rule = created[0] if created else ids[0]
    # What must never show up in B's answers (the probed id itself may be echoed in
    # a 404 message, so it is not a marker; its content hash is).
    a_rules = [r for r in a.registry.all_versions() if r.rule_id.startswith("a-")]
    markers = [
        a_head,
        *(r.content_hash for r in a_rules),
        *(r.rule_id for r in a_rules if r.rule_id != target_rule),
        *(rec.decision_sha256 for rec in a.decisions()),
    ]
    target_decision = a_decisions[-1].decision_id if a_decisions else "dec-00001"
    b_decisions = {d.decision.decision_id for d in w.gate(B).decisions()}
    a_job, _ = w.queue.submit(
        JobSubmit(
            kind=JobKind.GATE_EVALUATION,
            params={"rule_id": target_rule, "version": 1, "actor": "proposer-bank-a"},
        ),
        tenant_id=A,
        actor="proposer-bank-a",
    )
    markers.append(f'"job_id":"{a_job.job_id}"')

    for route in _routes(w):
        if {p.kind for p in protection_of(route)} != {"permission"}:
            continue  # public or self-authenticating: reveals no tenant data
        method = sorted(route.methods or {"GET"})[0].lower()
        for role, hdr in w.headers[B].items():
            path = _fill(route.path, target_rule, target_decision, a_job.job_id)
            body = _body(route, f"{role}-{B}")
            kw: dict[str, Any] = {"headers": hdr}
            if body is not None:
                kw["json"] = body
            r = getattr(w.client, method)(path, **kw)
            text = r.text
            leaked = [m for m in markers if m and m in text]
            assert not leaked, (route.path, role, r.status_code, leaked)
            if route.path in CREATES and method == "post":
                # creates in B's own tenant, or refused (empty body, no permission)
                assert r.status_code in (200, 201, 202, 403, 422), (role, r.status_code, text)
            elif route.path in LISTS:
                assert r.status_code in (200, 403), (route.path, role, r.status_code, text)
            elif route.path.startswith("/console/") and method == "post":
                # a console form without its CSRF token is refused before any lookup;
                # the cross-tenant approve with a valid token is in test_console_auth.py
                assert r.status_code in (403, 404, 415), (route.path, role, r.status_code)
            elif "{decision_id}" in route.path and target_decision in b_decisions:
                # B has its own decision with this number: B's, never A's (checked above)
                assert r.status_code in (200, 403, 404), (route.path, role, text)
            else:
                assert r.status_code in (403, 404), (route.path, role, r.status_code, text)
    assert a.audit.head() == a_head
    assert [d.decision for d in a.decisions()] == a_decisions
    # control: the same probes do reach the data with A's own credentials
    own = w.headers[A]["auditor"]
    if created:
        r = w.client.get(f"/rules/{target_rule}", headers=own)
        assert (
            r.status_code == 200
            and a_rules[0].content_hash in w.client.get("/rules", headers=own).text
        )
    assert w.client.get(f"/decisions/{target_decision}", headers=own).status_code == 200
    assert a_head in w.client.get("/audit/verify", headers=own).text


def test_tenant_comes_from_the_credential_not_the_request(world: World) -> None:
    w = world
    hdr = w.headers[B]["auditor"]
    r = w.client.get("/auth/whoami", headers={**hdr, "X-Scruple-Tenant": A})
    assert r.json()["tenant"] == B
    r = w.client.get("/rules?tenant=bank-a", headers=hdr)
    assert r.status_code == 422  # no such parameter


def test_each_tenant_has_its_own_chain_and_numbering(world: World) -> None:
    w = world
    for t in (A, B):
        v = w.client.get("/audit/verify", headers=w.headers[t]["auditor"]).json()
        assert v["ok"] is True
    b = w.gate(B)
    first = b.audit.entries()[0]
    assert first.seq == 1 and first.prev_hash == genesis_hash(B) != "0" * 64


def test_keys_are_listed_per_tenant(world: World) -> None:
    w = world
    _, tok = w.keys.create("admin-a", [], "local-admin", tenant=A, roles=[Role.RISK_ADMIN])
    rows = w.client.get("/auth/keys", headers={"X-API-Key": tok.get_secret_value()}).json()
    assert rows and {r["tenant"] for r in rows} == {A}
    assert json.dumps(rows).count(B) == 0


def test_approval_needs_the_tenants_own_named_approver(world: World) -> None:
    """An approver key of tenant B cannot touch tenant A's rule, even with the same name."""
    w = world
    a = w.gate(A)
    a.bootstrap_baseline()
    if not a.registry.versions("a-approval"):
        spec = GOOD_RULE.model_copy(update={"priority": 77})
        a.propose("a-approval", spec, "ops-team-a", Provenance(agent_id="proposer-bank-a"))
    rule = a.registry.versions("a-approval")[0]
    url = f"/rules/{rule.rule_id}/versions/{rule.version}/approve"
    r = w.client.post(url, headers=w.headers[B]["named"])
    assert r.status_code == 404
