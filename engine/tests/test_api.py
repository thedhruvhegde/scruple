from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

# These tests exercise the gate through the API, so they use the development-only
# X-Actor trust; tests/test_auth_*.py cover real authentication.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own

AGENT = {"X-Actor": "dispute-agent-01"}
RISK = {"X-Actor": "model-risk@demo.bank"}


@pytest.fixture
def client() -> TestClient:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=1_500, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    return TestClient(create_app(g, auth=DEV_AUTH), headers=READER)


def _body(rule_id: str, spec: object) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "spec": spec,
        "owner": "payments-ops",
        "provenance": {"agent_id": "dispute-agent-01", "episode_ids": ["ep-1"]},
    }


def test_full_flow_over_http(client: TestClient) -> None:
    r = client.post(
        "/rules", json=_body(GOOD_RULE_ID, GOOD_RULE.model_dump(mode="json")), headers=AGENT
    )
    assert r.status_code == 201, r.text
    r = client.post(f"/rules/{GOOD_RULE_ID}/versions/1/evaluate", headers=AGENT)
    assert [d["outcome"] for d in r.json()] == ["promote", "canary_passed"]
    assert (
        client.post(f"/rules/{GOOD_RULE_ID}/versions/1/approve", headers=AGENT).status_code == 403
    )  # agent cannot approve
    r = client.post(f"/rules/{GOOD_RULE_ID}/versions/1/approve", headers=RISK)
    assert r.json()["outcome"] == "activated"
    report = client.get(r.json()["report_url"])
    assert report.status_code == 200 and report.text.startswith("# Evidence report")
    r = client.post(f"/rules/{GOOD_RULE_ID}/rollback", json={"reason": "drill"}, headers=RISK)
    assert r.json()["outcome"] == "rolled_back"
    assert client.get("/audit/verify").json()["ok"] is True


def test_bad_rule_blocked_over_http(client: TestClient) -> None:
    client.post("/rules", json=_body(BAD_RULE_ID, BAD_RULE.model_dump(mode="json")), headers=AGENT)
    r = client.post(f"/rules/{BAD_RULE_ID}/versions/1/evaluate", headers=AGENT)
    assert r.json()[0]["outcome"] == "block"
    d = client.get(f"/decisions/{r.json()[0]['decision_id']}").json()
    assert d["shadow"]["candidate"]["policy_findings"] > 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["spec"]["conditions"][0].update({"field": "claim_valid"}),  # ground truth
        lambda b: b["spec"].update({"priority": -1}),
        lambda b: b["spec"].update({"unexpected": 1}),  # extra fields forbidden
        lambda b: b.update({"rule_id": "../../etc"}),
        lambda b: b["spec"].update({"conditions": []}),
        lambda b: b["spec"].update({"description": "x" * 501}),
    ],
)
def test_invalid_input_is_rejected(client: TestClient, mutate: object) -> None:
    body = _body("some-rule", GOOD_RULE.model_dump(mode="json"))
    mutate(body)  # type: ignore[operator]
    assert client.post("/rules", json=body, headers=AGENT).status_code == 422


def test_actor_header_required_and_validated(client: TestClient) -> None:
    body = _body("some-rule", GOOD_RULE.model_dump(mode="json"))
    del client.headers["X-Actor"]
    assert client.post("/rules", json=body).status_code == 401
    assert client.post("/rules", json=body, headers={"X-Actor": "bad actor!"}).status_code == 401
    assert client.post("/rules", json=body, headers={"X-Actor": "someone-else"}).status_code == 403


def test_errors_map_to_http_codes(client: TestClient) -> None:
    body = _body(GOOD_RULE_ID, GOOD_RULE.model_dump(mode="json"))
    client.post("/rules", json=body, headers=AGENT)
    assert client.post("/rules", json=body, headers=AGENT).status_code == 409
    assert client.post("/rules/nope-rule/versions/1/evaluate", headers=AGENT).status_code == 404
    assert client.get("/decisions/dec-99999").status_code == 404
    assert client.get("/rules/nope-rule").status_code == 404
    assert (
        client.post(
            "/rules/base-fx-contest/rollback", json={"reason": "why"}, headers=AGENT
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/rules/x/versions/9223372036854775808/evaluate"),
        ("post", "/rules/x/versions/9223372036854775808/approve"),
        ("get", "/shadow/rules/x/versions/9223372036854775808"),
        ("post", "/shadow/rules/x/versions/9223372036854775808/decisions"),
        ("post", "/rules/x/versions/0/evaluate"),
        ("get", "/shadow/rules/x/versions/-1"),
    ],
)
def test_out_of_range_versions_are_422_not_500(client: TestClient, method: str, path: str) -> None:
    """Regression (found by schemathesis, tests/test_api_fuzz.py): a version above
    2**63 - 1 reached SQLite and raised OverflowError, a 500."""
    kwargs: dict[str, object] = {"headers": RISK}
    if path.endswith("/decisions"):
        kwargs["json"] = {"decisions": []}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 422, r.text
    assert any(e["loc"][-1] == "version" for e in r.json()["detail"])
