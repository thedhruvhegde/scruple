"""Every API boundary rejects unknown fields: bodies, nested models, query, CLI files."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scruple.api.app import create_app
from scruple.api.strict import body_models, declared_query, takes_body
from scruple.auth.authn import Authenticator
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

# These tests exercise the gate through the API, so they use the development-only
# X-Actor trust; tests/test_auth_*.py cover real authentication.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own

AGENT = {"X-Actor": "dispute-agent-01"}
RISK = {"X-Actor": "model-risk@demo.bank"}
APP = create_app(AgentGate(SQLiteStorage(":memory:")), auth=DEV_AUTH)


@pytest.fixture
def client() -> TestClient:
    g = AgentGate(
        SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=800, bootstrap_resamples=100)
    )
    g.bootstrap_baseline()
    return TestClient(create_app(g, auth=DEV_AUTH), headers=READER)


def _body() -> dict[str, Any]:
    return {
        "rule_id": GOOD_RULE_ID,
        "spec": GOOD_RULE.model_dump(mode="json"),
        "owner": "payments-ops",
        "provenance": {"agent_id": "dispute-agent-01", "episode_ids": ["ep-1"]},
    }


def _nested_models(model: type[BaseModel]) -> set[type[BaseModel]]:
    """The model plus every BaseModel reachable through its field annotations."""
    seen: set[type[BaseModel]] = set()
    todo: list[object] = [model]
    while todo:
        t = todo.pop()
        if isinstance(t, type) and issubclass(t, BaseModel):
            if t in seen:
                continue
            seen.add(t)
            todo.extend(f.annotation for f in t.model_fields.values())
        elif get_origin(t) is not None or get_args(t):
            todo.extend(get_args(t))
    return seen


ROUTES = [r for r in APP.routes if isinstance(r, APIRoute)]


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{sorted(r.methods)[0]} {r.path}")
def test_every_body_model_forbids_extra_fields(route: APIRoute) -> None:
    for model in body_models(route):
        for m in _nested_models(model):
            assert m.model_config.get("extra") == "forbid", f"{m.__name__} in {route.path}"


def test_introspection_sees_the_real_inputs() -> None:
    by_path = {(r.path, sorted(r.methods or ())[0]): r for r in ROUTES}
    assert takes_body(by_path[("/rules", "POST")])
    assert not takes_body(by_path[("/rules/{rule_id}/versions/{version}/evaluate", "POST")])
    assert declared_query(by_path[("/decisions", "GET")]) == {"rule_id", "limit", "offset"}
    assert declared_query(by_path[("/rules", "GET")]) == {"limit", "offset"}
    assert declared_query(by_path[("/audit/verify", "GET")]) == {"expected_head"}
    assert body_models(by_path[("/rules", "POST")])[0].__name__ == "RegisterIn"


Mutation = Callable[[dict[str, Any]], None]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update({"extra": 1}),
        lambda b: b["spec"].update({"extra": 1}),
        lambda b: b["spec"]["conditions"][0].update({"extra": 1}),
        lambda b: b["provenance"].update({"extra": 1}),
        lambda b: b["provenance"].update({"trust_me": True, "score": 1.0}),
    ],
    ids=["top", "spec", "condition", "provenance", "provenance-two"],
)
def test_register_with_extra_fields_is_422(client: TestClient, mutate: Mutation) -> None:
    body = _body()
    mutate(body)
    r = client.post("/rules", json=body, headers=AGENT)
    assert r.status_code == 422, r.text
    assert any(e["type"] == "extra_forbidden" for e in r.json()["detail"])
    assert all(v["rule_id"] != GOOD_RULE_ID for v in client.get("/rules").json())


def test_rollback_with_extra_field_is_422(client: TestClient) -> None:
    r = client.post(
        "/rules/base-fx-contest/rollback", json={"reason": "drill", "force": True}, headers=RISK
    )
    assert r.status_code == 422
    assert client.get("/rules/base-fx-contest").json()[0]["status"] == "active"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("GET", "/version"),
        ("GET", "/rules"),
        ("GET", "/rules/base-fx-contest"),
        ("GET", "/decisions"),
        ("GET", "/audit/verify"),
        ("POST", "/rules/base-fx-contest/versions/1/evaluate"),
    ],
)
def test_unknown_query_parameter_is_422(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, path, params={"rule": "x"}, headers=AGENT)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "rule"]


def test_declared_query_parameters_still_work(client: TestClient) -> None:
    assert client.get("/decisions", params={"rule_id": "x"}).status_code == 200
    assert client.get("/audit/verify", params={"expected_head": "0" * 64}).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        f"/rules/{GOOD_RULE_ID}/versions/1/evaluate",
        f"/rules/{GOOD_RULE_ID}/versions/1/approve",
    ],
)
def test_body_on_a_bodiless_route_is_422(client: TestClient, path: str) -> None:
    client.post("/rules", json=_body(), headers=AGENT)
    r = client.post(path, json={"skip_checks": True}, headers=RISK)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body"]
    assert client.get("/decisions").json() == []  # nothing ran


def test_cli_propose_refuses_unknown_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "rule.json"
    body = _body()
    body["provenance"]["claimed_metrics_v2"] = {"x": 1}
    f.write_text(json.dumps(body))
    assert main(["propose", str(f), "--db", str(tmp_path / "g.db")]) == 2
    assert "Extra inputs are not permitted" in capsys.readouterr().err
