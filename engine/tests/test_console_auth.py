"""The console behind real API keys: RBAC, identity from the credential, tenancy.

The other console tests use the development-only X-Actor trust; these use hashed API
keys exactly as a deployment would. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.console.app import create_console_app
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE_ID
from tests.test_console_support import OPERATOR, SECRET, csrf_token, ready_gate, sources

BASE = f"/console/approvals/{GOOD_RULE_ID}/1"
VERSION = f"/console/rules/{GOOD_RULE_ID}/versions/1"


@pytest.fixture
def setup() -> Iterator[tuple[AgentGate, ApiKeyService]]:
    g = ready_gate()
    keys = ApiKeyService(g.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)
    yield g, keys
    g.storage.close()


def _key(keys: ApiKeyService, subject: str, role: Role, tenant: str | None = None) -> str:
    kw: dict[str, Any] = {"roles": [role]}
    if tenant is not None:
        kw["tenant"] = tenant
    _, token = keys.create(subject, (), "local-admin", **kw)
    return token.get_secret_value()


@pytest.fixture(params=["mounted", "standalone"])
def app(request: pytest.FixtureRequest, setup: tuple[AgentGate, ApiKeyService]) -> Any:
    g, keys = setup
    if request.param == "mounted":
        return create_app(g, auth=Authenticator(keys))
    return create_console_app(g, sources(), secret=SECRET, auth=Authenticator(keys))


def _client(app: Any, token: str | None) -> TestClient:
    return TestClient(app, headers={"X-API-Key": token} if token else {})


def test_no_credentials_is_401_but_static_files_are_public(app: Any) -> None:
    c = _client(app, None)
    for path in ("/console/", "/console/approvals", VERSION, f"{VERSION}/evidence.json"):
        assert c.get(path).status_code == 401, path
    assert c.get("/console/static/console.css").status_code == 200


def test_auditor_reads_and_exports_but_cannot_approve(
    app: Any, setup: tuple[AgentGate, ApiKeyService]
) -> None:
    g, keys = setup
    c = _client(app, _key(keys, "auditor@demo.bank", Role.AUDITOR))
    page = c.get("/console/approvals")
    assert page.status_code == 200 and "acting as auditor@demo.bank" in page.text
    assert c.get(VERSION).status_code == 200
    assert c.get(f"{VERSION}/evidence.json").status_code == 200  # export maps to read
    token = csrf_token(c)
    n = len(g.audit.entries())
    r = c.post(f"{BASE}/approve", data={"csrf": token, "reason": "looks right to me"})
    assert r.status_code == 403 and "approve" in r.json()["detail"]
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.CANARY
    assert len(g.audit.entries()) == n


def test_approver_acts_as_its_own_subject(app: Any, setup: tuple[AgentGate, ApiKeyService]) -> None:
    g, keys = setup
    c = _client(app, _key(keys, OPERATOR, Role.APPROVER))
    token = csrf_token(c)
    r = c.post(
        f"{BASE}/approve",
        data={"csrf": token, "reason": "reviewed the evidence pack"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE
    assert g.decisions(GOOD_RULE_ID)[-1].decision.actor == OPERATOR


def test_a_proposer_key_cannot_approve(app: Any, setup: tuple[AgentGate, ApiKeyService]) -> None:
    _, keys = setup
    c = _client(app, _key(keys, "agent@demo.bank", Role.PROPOSER))
    token = csrf_token(c)
    r = c.post(f"{BASE}/approve", data={"csrf": token, "reason": "my own rule is fine"})
    assert r.status_code == 403


def test_another_tenant_sees_none_of_this_tenants_rules(
    app: Any, setup: tuple[AgentGate, ApiKeyService]
) -> None:
    _, keys = setup
    c = _client(app, _key(keys, "auditor@other.bank", Role.AUDITOR, tenant="other-bank"))
    r = c.get("/console/approvals")
    if r.status_code == 200:  # mounted: the other tenant's own (empty) inbox
        assert GOOD_RULE_ID not in r.text
        assert c.get(VERSION).status_code == 404
    else:  # standalone: one database, one tenant; nobody else gets in
        assert r.status_code == 404
    assert c.get(f"{VERSION}/evidence.json").status_code == 404
