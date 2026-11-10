"""RBAC: every route declares a permission, roles grant what the table says, and
separation of duties holds with real identities."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth import Permission, Principal, require
from scruple.auth.authn import Authenticator
from scruple.auth.deps import iter_routes, protection_of
from scruple.auth.keys import ApiKeyService, InvalidKeyError, ScryptParams
from scruple.auth.rbac import (
    ROLE_PERMISSIONS,
    Role,
    SeparationOfDutiesError,
    check_separation,
    key_permissions,
    permissions_for,
)
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

P = Permission
FAST = ScryptParams(n=1024)
PUBLIC = {"/health", "/version", "/console/static/{name:path}"}
SIGNED = {"/ingest/stripe/webhook"}


@pytest.fixture
def env() -> Iterator[tuple[TestClient, ApiKeyService, AgentGate]]:
    db = SQLiteStorage(":memory:")
    # The agent and the owner are named approvers here on purpose: the gate must still
    # refuse them on their own rule, by identity.
    cfg = GateConfig(
        n_cases=1_500,
        bootstrap_resamples=300,
        approvers=("model-risk@demo.bank", "ops-team", PROV.agent_id),
    )
    g = AgentGate(db, SteppingClock(), cfg)
    g.bootstrap_baseline()
    keys = ApiKeyService(db, params=FAST, permissions_for=key_permissions)
    yield TestClient(create_app(g, auth=Authenticator(keys))), keys, g
    db.close()


def _routes() -> list[Any]:
    app = create_app(AgentGate(SQLiteStorage(":memory:")), auth=Authenticator())
    return list(iter_routes(app))


ROUTES = _routes()


def _hdr(keys: ApiKeyService, subject: str, *, roles: Any = (), scopes: Any = ()) -> dict[str, str]:
    _, token = keys.create(subject, scopes, "local-admin", roles=roles)
    return {"X-API-Key": token.get_secret_value()}


def _method(route: Any) -> str:
    methods: set[str] = route.methods or {"GET"}
    return sorted(methods)[0]


def _concrete(path: str) -> str:
    values = {
        "rule_id": "baseline-x",
        "version": "1",
        "decision_id": "dec-00001",
        "episode_id": "ep-000000000000",
        "job_id": "job-00000001",
    }
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), "x"), path)


def _needed(route: Any) -> Permission:
    (perm,) = {r.permission for r in protection_of(route) if r.kind == "permission"}
    assert perm is not None
    return perm


# --- every route declares what it needs ---------------------------------------------


def test_every_route_declares_a_protection() -> None:
    assert len(ROUTES) >= 18
    for r in ROUTES:
        kinds = {p.kind for p in protection_of(r)}
        assert kinds, f"{r.path} declares no permission"
        assert len(kinds) == 1, f"{r.path} declares conflicting protections {kinds}"
        if r.path in PUBLIC:
            assert kinds == {"public"}, r.path
        elif r.path in SIGNED:
            assert kinds == {"signed"}, r.path
        else:
            assert kinds == {"permission"}, f"{r.path} is unprotected"
    assert {r.path for r in ROUTES if protection_of(r)[0].kind == "public"} == PUBLIC


@pytest.mark.parametrize(
    "route",
    [r for r in ROUTES if r.path not in PUBLIC | SIGNED],
    ids=lambda r: f"{_method(r)} {r.path}",
)
def test_each_protected_route_refuses_without_its_permission(
    env: tuple[TestClient, ApiKeyService, AgentGate], route: Any
) -> None:
    c, keys, _ = env
    method, path = _method(route).lower(), _concrete(route.path)
    need = _needed(route)
    kw: dict[str, Any] = {"json": {}} if method == "post" else {}
    r = getattr(c, method)(path, **kw)
    assert r.status_code == 401, (path, r.text)
    for other in Permission:
        if other is need:
            continue
        r = getattr(c, method)(path, headers=_hdr(keys, "probe", scopes=[other]), **kw)
        assert r.status_code == 403, (path, other, r.text)
        assert need.value in r.json()["detail"]
    r = getattr(c, method)(path, headers=_hdr(keys, "model-risk@demo.bank", scopes=[need]), **kw)
    assert r.status_code not in (401, 403) or "permission" not in r.text, (path, r.text)


def test_public_routes_answer_anonymously(env: tuple[TestClient, ApiKeyService, AgentGate]) -> None:
    c, _, _ = env
    for path in PUBLIC:
        assert c.get(path.replace("{name:path}", "console.css")).status_code == 200


def test_the_openapi_schema_needs_a_reader(
    env: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, keys, _ = env
    assert c.get("/openapi.json").status_code == 401
    assert c.get("/docs").status_code == 404
    r = c.get("/openapi.json", headers=_hdr(keys, "a", roles=[Role.AUDITOR]))
    assert r.status_code == 200 and "/rules" in r.json()["paths"]


# --- one line to protect a new route; undeclared routes fail closed -----------------


def test_a_new_router_adopts_protection_in_one_line(
    env: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, keys, _ = env
    router = APIRouter(prefix="/console", dependencies=[require(P.READ)])

    @router.get("/hello")
    def hello() -> dict[str, str]:
        return {"hi": "there"}

    @router.post("/act")
    def act(p: Annotated[Principal, require(P.ADMIN)]) -> dict[str, str]:
        return {"by": p.subject}

    app: Any = c.app
    app.include_router(router)
    assert c.get("/console/hello").status_code == 401
    reader = _hdr(keys, "reader", roles=[Role.AUDITOR])
    admin = _hdr(keys, "boss", roles=[Role.RISK_ADMIN])
    assert c.get("/console/hello", headers=reader).json() == {"hi": "there"}
    assert c.post("/console/act", headers=reader).status_code == 403
    assert c.post("/console/act", headers=admin).json() == {"by": "boss"}


def test_an_undeclared_route_is_closed_even_to_everyone(
    env: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, keys, _ = env

    app = cast(FastAPI, c.app)

    @app.get("/oops")
    def oops() -> dict[str, str]:
        return {"leak": "yes"}

    admin = _hdr(keys, "boss", roles=[Role.RISK_ADMIN])
    r = c.get("/oops", headers=admin)
    assert r.status_code == 403 and "declares no permission" in r.json()["detail"]
    dev = TestClient(c.app)
    assert dev.get("/oops").status_code == 403


# --- the role table -----------------------------------------------------------------


def test_role_table() -> None:
    assert {
        Role.PROPOSER: {P.READ, P.PROPOSE},
        Role.SERVICE: {P.READ, P.SHADOW},
        Role.APPROVER: {P.READ, P.APPROVE},
        Role.RISK_ADMIN: {P.READ, P.APPROVE, P.ADMIN},
        Role.AUDITOR: {P.READ},
    } == ROLE_PERMISSIONS
    for perms in ROLE_PERMISSIONS.values():
        check_separation(perms)  # no role breaks separation of duties alone


ROLE_NAMES = st.lists(st.sampled_from([*(r.value for r in Role), "root", "admin"]), max_size=4)
SCOPE_NAMES = st.lists(st.sampled_from([*(p.value for p in Permission), "root", "*"]), max_size=5)


@given(ROLE_NAMES, SCOPE_NAMES)
def test_permissions_never_exceed_roles_or_scopes(roles: list[str], scopes: list[str]) -> None:
    got = permissions_for(roles, scopes)
    from_roles = frozenset().union(
        *(ROLE_PERMISSIONS[Role(r)] for r in roles if r in {x.value for x in Role})
    )
    named = {p for p in Permission if p.value in scopes}
    if roles:
        assert got <= from_roles
    if named:  # scope strings that are not permissions (openid, ...) are ignored
        assert got <= named
    if not from_roles and not named:
        assert got == frozenset()


def test_auditor_is_read_only() -> None:
    assert permissions_for(["auditor"], []) == {P.READ}
    assert permissions_for(["auditor"], ["admin"]) == frozenset()  # scopes cannot widen


# --- separation of duties -----------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "scopes"),
    [
        ([Role.PROPOSER, Role.APPROVER], []),
        ([Role.PROPOSER, Role.RISK_ADMIN], []),
        ([], [P.PROPOSE, P.APPROVE]),
        ([Role.PROPOSER, Role.APPROVER], [P.PROPOSE, P.APPROVE, P.READ]),
    ],
)
def test_one_key_cannot_propose_and_approve(roles: Any, scopes: Any) -> None:
    keys = ApiKeyService(SQLiteStorage(":memory:"), params=FAST, permissions_for=key_permissions)
    with pytest.raises(SeparationOfDutiesError):
        keys.create("both-hats", scopes, "local-admin", roles=roles)


def test_a_stored_key_that_breaks_separation_does_not_authenticate() -> None:
    """Created before the rule, or written behind the service's back: refused."""
    db = SQLiteStorage(":memory:")
    loose = ApiKeyService(db, params=FAST)  # scope-only check, as before RBAC
    _, token = loose.create("both-hats", [P.PROPOSE, P.APPROVE], "local-admin")
    strict = ApiKeyService(db, params=FAST, permissions_for=key_permissions)
    with pytest.raises(InvalidKeyError):
        strict.authenticate(token.get_secret_value())


def test_separation_of_duties_with_real_identities(
    env: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, keys, g = env
    agent = _hdr(keys, PROV.agent_id, roles=[Role.PROPOSER])
    body = {
        "rule_id": GOOD_RULE_ID,
        "spec": GOOD_RULE.model_dump(mode="json"),
        "owner": "ops-team",
        "provenance": PROV.model_dump(mode="json"),
    }
    impostor = _hdr(keys, "not-the-agent", roles=[Role.PROPOSER])
    assert c.post("/rules", json=body, headers=impostor).status_code == 403
    assert c.post("/rules", json=body, headers=agent).status_code == 201
    r = c.post(f"/rules/{GOOD_RULE_ID}/versions/1/evaluate", headers=agent)
    assert [d["outcome"] for d in r.json()] == ["promote", "canary_passed"]

    url = f"/rules/{GOOD_RULE_ID}/versions/1/approve"
    assert c.post(url, headers=agent).status_code == 403  # proposer role: no approve
    # an approver credential issued to the agent's own identity: the gate refuses it
    agent_as_approver = _hdr(keys, PROV.agent_id, roles=[Role.APPROVER])
    r = c.post(url, headers=agent_as_approver)
    assert r.status_code == 403 and "segregation" in r.json()["detail"]
    owner = _hdr(keys, "ops-team", roles=[Role.APPROVER])
    r = c.post(url, headers=owner)
    assert r.status_code == 403 and "segregation" in r.json()["detail"]
    stranger = _hdr(keys, "random-approver", roles=[Role.APPROVER])
    r = c.post(url, headers=stranger)
    assert r.status_code == 403 and "not an authorised approver" in r.json()["detail"]
    named = _hdr(keys, "model-risk@demo.bank", roles=[Role.APPROVER])
    r = c.post(url, headers=named)
    assert r.status_code == 200 and r.json()["outcome"] == "activated"
    assert g.decisions()[-1].decision.actor == "model-risk@demo.bank"


def test_auditor_reads_everything_and_changes_nothing(
    env: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, keys, g = env
    auditor = _hdr(keys, "examiner", roles=[Role.AUDITOR])
    head = g.audit.head()
    for r in ROUTES:
        method = _method(r).lower()
        if r.path in PUBLIC | SIGNED:
            continue
        resp = getattr(c, method)(_concrete(r.path), headers=auditor)
        if _needed(r) is P.READ:
            assert method == "get"
            assert resp.status_code in (200, 404), (r.path, resp.text)
        else:
            assert resp.status_code == 403, (r.path, resp.text)
    assert g.audit.head() == head


def test_cli_creates_keys_with_roles(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCRUPLE_AUTH_SCRYPT_N", "1024")
    db = str(tmp_path / "k.db")
    assert main(["keys", "create", "--db", db, "--subject", "ex", "--role", "auditor"]) == 0
    capsys.readouterr()
    both = ["--role", "proposer", "--role", "approver"]
    assert main(["keys", "create", "--db", db, "--subject", "x", *both]) == 2
    assert "separation of duties" in capsys.readouterr().err
    assert main(["keys", "list", "--db", db]) == 0
    assert "auditor" in capsys.readouterr().out
