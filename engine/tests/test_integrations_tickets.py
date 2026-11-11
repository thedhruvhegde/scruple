"""Jira and Linear change tickets: one issue per candidate, one update per decision,
idempotent by decision id. Fake trackers behind an httpx.MockTransport; synthetic data."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from scruple.integrations.config import (
    IntegrationSettings,
    JiraConfig,
    LinearConfig,
    SecretError,
    SlackApprover,
    resolve_secret,
    tenant_secret_prefix,
)
from scruple.integrations.dispatch import dispatch
from scruple.integrations.jobs import make_handlers
from scruple.integrations.ops import set_settings
from scruple.integrations.runtime import Runtime
from scruple.integrations.safehttp import SafeHttp
from scruple.integrations.store import GateEvent, IntegrationStore
from scruple.integrations.tickets import (
    TransientTicketError,
    connector_for,
    marker,
    stable_uuid,
    sync,
)
from scruple.jobs.queue import queue_for
from scruple.jobs.worker import JobContext, PermanentJobError, Worker
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

T0 = datetime(2026, 11, 1, tzinfo=UTC)
JIRA_TOKEN = "SCRUPLE_SECRET_DEFAULT_JIRA"
LINEAR_KEY = "SCRUPLE_SECRET_DEFAULT_LINEAR"
ENV = {JIRA_TOKEN: "jira-token-value", LINEAR_KEY: "lin_api_value"}
JIRA = JiraConfig(
    base_url="https://acme.atlassian.net",
    email="bot@acme.example",
    api_token_env=JIRA_TOKEN,
    project_key="RISK",
)
TEAM = "9cfb482a-81e3-4154-b5b9-2c805e70a02d"
LINEAR = LinearConfig(api_key_env=LINEAR_KEY, team_id=TEAM)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t


class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[Any]] = {}
        self.requests: list[httpx.Request] = []
        self.fail: list[int] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["host"] == "acme.atlassian.net"
        auth = base64.b64decode(req.headers["authorization"].removeprefix("Basic ")).decode()
        assert auth == "bot@acme.example:jira-token-value"
        if self.fail:
            return httpx.Response(self.fail.pop(0), json={"errorMessages": ["nope"]})
        path = req.url.path
        if req.method == "POST" and path == "/rest/api/3/issue":
            fields = json.loads(req.content)["fields"]
            n = len(self.issues) + 1
            key = f"RISK-{n}"
            self.issues[key] = {"id": str(10_000 + n), "key": key, "fields": fields}
            return httpx.Response(201, json={"id": str(10_000 + n), "key": key, "self": "x"})
        if req.method == "POST" and path.endswith("/comment"):
            key = path.split("/")[-2]
            self.comments.setdefault(key, []).append(json.loads(req.content)["body"])
            return httpx.Response(201, json={"id": "1"})
        if req.method == "GET" and path == "/rest/api/3/search/jql":
            jql = parse_qs(urlsplit(str(req.url)).query)["jql"][0]
            hits = [
                {"id": i["id"], "key": k}
                for k, i in self.issues.items()
                if f'"{i["fields"]["labels"][-1]}"' in jql
            ]
            return httpx.Response(200, json={"issues": hits[:1]})
        return httpx.Response(404)


class FakeLinear:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.fail: list[int] = []
        self.lose_response = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert str(req.url).endswith("/graphql") and req.headers["host"] == "api.linear.app"
        assert req.headers["authorization"] == "lin_api_value"  # personal keys: no Bearer
        body = json.loads(req.content)
        self.requests.append(body)
        if self.fail:
            return httpx.Response(self.fail.pop(0))
        q, v = body["query"], body["variables"]
        if q.startswith("mutation IssueCreate"):
            inp = v["input"]
            if inp["id"] in self.issues:
                return httpx.Response(
                    200, json={"data": None, "errors": [{"message": "duplicate id"}]}
                )
            n = len(self.issues) + 1
            issue = {"id": inp["id"], "identifier": f"RSK-{n}", "url": f"https://linear.app/i/{n}"}
            self.issues[inp["id"]] = {**issue, "input": inp}
            if self.lose_response:
                self.lose_response = False
                return httpx.Response(502)
            return httpx.Response(
                200, json={"data": {"issueCreate": {"success": True, "issue": issue}}}
            )
        if q.startswith("mutation CommentCreate"):
            inp = v["input"]
            if inp["id"] in self.comments:
                return httpx.Response(
                    200, json={"data": None, "errors": [{"message": "Comment already exists"}]}
                )
            self.comments[inp["id"]] = inp
            return httpx.Response(
                200,
                json={"data": {"commentCreate": {"success": True, "comment": {"id": inp["id"]}}}},
            )
        if q.startswith("query Issue"):
            i = self.issues.get(v["id"])
            if i is None:
                return httpx.Response(
                    200,
                    json={
                        "data": {"issue": None},
                        "errors": [{"message": "Entity not found: Issue"}],
                    },
                )
            return httpx.Response(
                200, json={"data": {"issue": {k: i[k] for k in ("id", "identifier", "url")}}}
            )
        return httpx.Response(400)


class Trackers:
    def __init__(self) -> None:
        self.jira, self.linear = FakeJira(), FakeLinear()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.headers["host"] == "api.linear.app":
            return self.linear(req)
        return self.jira(req)


def runtime(t: Trackers, dns: Any = None) -> Runtime:
    return Runtime(
        env=ENV,
        http=SafeHttp(
            transport=httpx.MockTransport(t), resolver=dns or (lambda h, p: ["93.184.215.14"])
        ),
    )


def _event(store: IntegrationStore, n: int, rule: str = "r-1", version: int = 1) -> str:
    e = GateEvent(
        id=f"evt_{n:024d}",
        type="decision.recorded",
        tenant=store.tenant,
        created_at=T0,
        audit_seq=n,
        audit_entry_hash="h" * 64,
        data={
            "decision_id": f"dec-{n:05d}",
            "stage": "shadow" if n == 1 else "canary",
            "outcome": "promote",
            "rule_id": rule,
            "version": version,
            "content_hash": "c" * 64,
            "decision_sha256": "d" * 64,
            "summary": f"decision {n}",
            "reasons": ["reason a", "reason b"],
            "report_path": f"/decisions/dec-{n:05d}/report",
            "data": "synthetic",
        },
    )
    store.ensure().put_event(e)
    return e.id


@pytest.fixture
def store() -> IntegrationStore:
    db = SQLiteStorage(":memory:")
    s = IntegrationStore(db).ensure()
    STORAGES[id(s)] = db
    s.put_settings(IntegrationSettings(jira=JIRA, linear=LINEAR), "admin", T0)
    return s


# --- Jira ---------------------------------------------------------------------------


def test_jira_issue_per_candidate_and_comment_per_decision(store: IntegrationStore) -> None:
    t = Trackers()
    rt = runtime(t)
    first = sync(_s(store), rt, "jira", _event(store, 1))
    assert first["created"] and first["issue"] == "RISK-1"
    assert first["url"] == "https://acme.atlassian.net/browse/RISK-1"
    issue = t.jira.issues["RISK-1"]["fields"]
    assert issue["project"] == {"key": "RISK"} and issue["issuetype"] == {"name": "Task"}
    assert issue["summary"] == "scruple change: r-1 v1"
    assert issue["labels"] == ["scruple", marker("default", "r-1@v1")]
    doc = issue["description"]
    assert doc["type"] == "doc" and doc["version"] == 1
    texts = [p["content"][0]["text"] for p in doc["content"]]
    assert texts[0].startswith("Decision dec-00001: shadow stage") and "- reason a" in texts
    assert any("synthetic" in x for x in texts)
    second = sync(_s(store), rt, "jira", _event(store, 2))
    assert not second["created"] and second["issue"] == "RISK-1"
    assert len(t.jira.comments["RISK-1"]) == 1
    # idempotent by decision id: a repeat sends nothing
    before = len(t.jira.requests)
    assert sync(_s(store), rt, "jira", _event(store, 2))["skipped"]
    assert len(t.jira.requests) == before
    # another candidate gets its own issue
    assert sync(_s(store), rt, "jira", _event(store, 3, "r-2"))["issue"] == "RISK-2"
    assert [r["issue_key"] for r in store.tickets()] == ["RISK-1", "RISK-2"]


def _s(store: IntegrationStore) -> SQLiteStorage:
    return STORAGES[id(store)]


STORAGES: dict[int, SQLiteStorage] = {}


def test_a_concurrent_creation_waits_and_a_stale_one_is_taken_over(
    store: IntegrationStore,
) -> None:
    t = Trackers()
    rt, clock = runtime(t), Clock()
    assert store.claim_ticket("jira", "r-1@v1", T0)  # another worker is creating it
    with pytest.raises(TransientTicketError, match="another job"):
        sync(_s(store), rt, "jira", _event(store, 1), clock=clock)
    assert t.jira.requests == []
    # that worker created the issue and crashed before recording it
    t.jira.issues["RISK-1"] = {
        "id": "10001",
        "key": "RISK-1",
        "fields": {"labels": ["scruple", marker("default", "r-1@v1")]},
    }
    clock.t = T0 + timedelta(minutes=3)
    out = sync(_s(store), rt, "jira", _event(store, 1), clock=clock)
    assert out["issue"] == "RISK-1" and not out["created"]
    assert len(t.jira.issues) == 1 and len(t.jira.comments["RISK-1"]) == 1


def test_a_failed_creation_releases_its_claim(store: IntegrationStore) -> None:
    t = Trackers()
    rt = runtime(t)
    t.jira.fail = [503]
    with pytest.raises(TransientTicketError, match="HTTP 503"):
        sync(_s(store), rt, "jira", _event(store, 1))
    assert store.ticket("jira", "r-1@v1") is None
    assert sync(_s(store), rt, "jira", _event(store, 1))["created"]


@pytest.mark.parametrize(
    ("code", "permanent"), [(401, True), (404, True), (429, False), (500, False)]
)
def test_jira_errors(store: IntegrationStore, code: int, permanent: bool) -> None:
    t = Trackers()
    t.jira.fail = [code]
    with pytest.raises(PermanentJobError if permanent else TransientTicketError):
        sync(_s(store), runtime(t), "jira", _event(store, 1))


def test_network_errors_are_transient(store: IntegrationStore) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    rt = Runtime(
        env=ENV,
        http=SafeHttp(transport=httpx.MockTransport(boom), resolver=lambda h, p: ["93.184.215.14"]),
    )
    with pytest.raises(TransientTicketError, match="ReadTimeout"):
        sync(_s(store), rt, "jira", _event(store, 1))


def test_a_jira_site_on_a_private_address_is_refused(store: IntegrationStore) -> None:
    t = Trackers()
    with pytest.raises(PermanentJobError, match="refused"):
        sync(_s(store), runtime(t, dns=lambda h, p: ["10.0.0.7"]), "jira", _event(store, 1))
    assert t.jira.requests == []


# --- Linear -------------------------------------------------------------------------


def test_linear_issue_and_comments_use_stable_ids(store: IntegrationStore) -> None:
    t = Trackers()
    rt = runtime(t)
    out = sync(_s(store), rt, "linear", _event(store, 1))
    assert out["created"] and out["issue"] == "RSK-1"
    create = next(r for r in t.linear.requests if r["query"].startswith("mutation IssueCreate"))
    inp = create["variables"]["input"]
    assert inp["id"] == stable_uuid("default", "r-1@v1") and inp["teamId"] == TEAM
    assert inp["title"] == "scruple change: r-1 v1" and "dec-00001" in inp["description"]
    sync(_s(store), rt, "linear", _event(store, 2))
    (comment,) = t.linear.comments.values()
    assert comment["id"] == stable_uuid("default", "comment", "dec-00002")
    assert comment["issueId"] == inp["id"]
    # the tracker already has this decision's comment (an attempt whose answer was lost)
    _s(store).connection.execute("DELETE FROM ticket_updates WHERE decision_id='dec-00002'")
    assert sync(_s(store), rt, "linear", _event(store, 2))["issue"] == "RSK-1"
    assert len(t.linear.comments) == 1


def test_linear_lost_create_response_never_duplicates(store: IntegrationStore) -> None:
    t = Trackers()
    rt = runtime(t)
    t.linear.lose_response = True  # created, but the answer is a 502
    with pytest.raises(TransientTicketError):
        sync(_s(store), rt, "linear", _event(store, 1))
    out = sync(_s(store), rt, "linear", _event(store, 1))  # the retry
    assert out["issue"] == "RSK-1" and len(t.linear.issues) == 1


def test_uuid_is_version_four_and_stable() -> None:
    import uuid

    u = uuid.UUID(stable_uuid("a", "b"))
    assert u.version == 4 and u.variant == uuid.RFC_4122
    assert stable_uuid("a", "b") == stable_uuid("a", "b") != stable_uuid("a", "c")


# --- configuration and secrets ----------------------------------------------------------


def test_secrets_must_be_the_tenants_own() -> None:
    assert tenant_secret_prefix("bank-a") == "SCRUPLE_SECRET_BANK_A_"
    env = {"SCRUPLE_SECRET_BANK_A_JIRA": "v", "SCRUPLE_DATABASE_PASSWORD": "pw"}
    assert resolve_secret("SCRUPLE_SECRET_BANK_A_JIRA", "bank-a", env) == "v"
    for name, tenant in (
        ("SCRUPLE_SECRET_BANK_A_JIRA", "bank-b"),
        ("SCRUPLE_DATABASE_PASSWORD", "bank-a"),
        ("SCRUPLE_SECRET_BANK_A_MISSING", "bank-a"),
    ):
        with pytest.raises(SecretError) as exc:
            resolve_secret(name, tenant, env)
        assert "pw" not in str(exc.value)
    s = IntegrationSettings(jira=JIRA)
    s.check_secret_names("default")
    with pytest.raises(ValueError, match="SCRUPLE_SECRET_BANK_A_"):
        s.check_secret_names("bank-a")


def test_unconfigured_or_secretless_trackers_fail_permanently(store: IntegrationStore) -> None:
    rt = Runtime(env={}, http=SafeHttp(resolver=lambda h, p: ["93.184.215.14"]))
    with pytest.raises(PermanentJobError, match="not set"):
        connector_for("jira", store.settings(), rt, "default")
    with pytest.raises(PermanentJobError, match="not configured"):
        connector_for("jira", IntegrationSettings(), rt, "default")
    with pytest.raises(PermanentJobError, match="no decision event"):
        sync(_s(store), rt, "jira", "evt_missing")
    assert "token" not in repr(
        connector_for("jira", store.settings(), runtime(Trackers()), "default")
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"project_key": "risk"},
        {"base_url": "http://acme.atlassian.net"},
        {"labels": ["has space"]},
        {"api_token_env": "DATABASE_PASSWORD"},
        {"email": "not-an-email"},
    ],
)
def test_jira_config_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JiraConfig.model_validate({**JIRA.model_dump(), **bad})


@pytest.mark.parametrize(
    ("roles", "ok"),
    [(("approver",), True), (("risk_admin",), True), (("auditor",), False), (("proposer",), False)],
)
def test_slack_approvers_must_hold_approve(roles: tuple[str, ...], ok: bool) -> None:
    kw = {"slack_user_id": "U0001", "subject": "alice@bank.example", "roles": roles}
    if ok:
        assert SlackApprover.model_validate(kw)
    else:
        with pytest.raises(ValidationError):
            SlackApprover.model_validate(kw)


# --- end to end through the dispatcher and a worker ---------------------------------------


def test_dispatch_to_jira_through_the_worker(tmp_path: Path) -> None:
    path = str(tmp_path / "g.db")
    storage = SQLiteStorage(path)
    clock = Clock()
    gate = AgentGate(storage, clock)
    gate.bootstrap_baseline()
    set_settings(gate, IntegrationSettings(jira=JIRA), "admin")
    rules = [r.rule_id for r in gate.registry.active_rules()][:2]
    decisions = [gate.rollback(r, "model-risk@demo.bank", "ticket test").decision for r in rules]
    queue = queue_for(storage, clock=clock)
    t = Trackers()
    res = dispatch(gate, queue)
    assert res.jobs == len(decisions)
    worker = Worker(
        queue,
        JobContext(database=path),
        handlers=make_handlers(runtime(t), clock),
        isolation="thread",
    )
    while worker.run_once() is not None:
        pass
    assert len(t.jira.issues) == len(decisions)
    summaries = sorted(i["fields"]["summary"] for i in t.jira.issues.values())
    assert summaries == sorted(f"scruple change: {d.rule_id} v{d.version}" for d in decisions)
    assert dispatch(gate, queue).jobs == 0
    assert all(j.status.value == "succeeded" for j in queue.list_jobs())
    assert gate.verify_integrity().ok
