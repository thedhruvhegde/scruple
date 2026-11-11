"""Slack approvals: Block Kit requests, interactive buttons verified with Slack's signing
scheme, signed single-use approval links, identity mapping, separation of duties and
audit. Synthetic gate data; Slack is an httpx.MockTransport."""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import key_permissions
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.config import GateConfig
from scruple.gate.models import Outcome
from scruple.integrations.config import IntegrationSettings, SlackApprover, SlackConfig
from scruple.integrations.dispatch import dispatch
from scruple.integrations.jobs import make_handlers
from scruple.integrations.ops import set_settings
from scruple.integrations.runtime import Runtime
from scruple.integrations.safehttp import SafeHttp
from scruple.integrations.signing import LinkSigner, slack_signature
from scruple.integrations.slack import (
    APPROVE_ACTION,
    REJECT_ACTION,
    TransientSlackError,
    approval_message,
    notify,
)
from scruple.integrations.store import IntegrationStore
from scruple.jobs.queue import queue_for
from scruple.jobs.worker import JobContext, PermanentJobError, Worker
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import APPROVER, PROV

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
NOW = T0.timestamp()
ALICE = NamedApprover(name="alice@bank.example", roles=("model-risk",))
BOB = NamedApprover(name="bob@bank.example", roles=("ops-lead",))
POLICY = ApprovalPolicy(approvers=(ALICE, BOB), required=2, required_role="model-risk")
PROPOSER = "proposer@bank.example"
SIGNING = "slack-signing-secret"
ENV = {
    "SCRUPLE_SECRET_DEFAULT_SLACK_BOT": "xoxb-test",
    "SCRUPLE_SECRET_DEFAULT_SLACK_SIGNING": SIGNING,
}
SLACK = SlackConfig(
    team_id="T0001",
    channel="C0001",
    bot_token_env="SCRUPLE_SECRET_DEFAULT_SLACK_BOT",
    signing_secret_envs=("SCRUPLE_SECRET_DEFAULT_SLACK_SIGNING",),
    links=True,
    public_base_url="https://scruple.example.com",
    approvers=(
        SlackApprover(slack_user_id="UALICE", subject=ALICE.name),
        SlackApprover(slack_user_id="UBOB", subject=BOB.name, roles=("risk_admin",)),
        SlackApprover(slack_user_id="UPROP", subject=PROPOSER),
        SlackApprover(slack_user_id="UDAVE", subject="dave@bank.example"),
    ),
)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t


class Slack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any] | int] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.headers["host"] == "hooks.slack.com":
            self.responses.append(json.loads(req.content))
            return httpx.Response(200, text="ok")
        assert req.url.path == "/api/chat.postMessage"
        assert req.headers["authorization"] == "Bearer xoxb-test"
        nxt = self.answers.pop(0) if self.answers else {"ok": True, "ts": "1.2"}
        if isinstance(nxt, int):
            return httpx.Response(nxt)
        if nxt.get("ok"):
            self.posts.append(json.loads(req.content))
        return httpx.Response(200, json=nxt)


@cache
def _template() -> Path:
    """One gate with a version that passed canary (evaluated once per test session)."""
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="scruple-slack-")) / "gate.db"
    s = SQLiteStorage(str(path))
    g = AgentGate(s, Clock(), GateConfig(n_cases=2_000, bootstrap_resamples=300))
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    assert g.evaluate(GOOD_RULE_ID, 1)[-1].decision.outcome is Outcome.CANARY_PASSED
    g.set_approval_policy(POLICY, APPROVER)
    s.close()
    return path


class World:
    def __init__(self, tmp: Path, *, proposer: str = PROPOSER, links: bool = True) -> None:
        self.path = str(tmp / "gate.db")
        shutil.copy(_template(), self.path)
        self.clock = Clock()
        self.storage = SQLiteStorage(self.path)
        self.gate = AgentGate(
            self.storage, self.clock, GateConfig(n_cases=2_000, bootstrap_resamples=300)
        )
        self.slack = Slack()
        self.runtime = Runtime(
            env=ENV,
            http=SafeHttp(
                transport=httpx.MockTransport(self.slack), resolver=lambda h, p: ["93.184.215.14"]
            ),
            links=LinkSigner({"k1": b"\x05" * 32}, "k1") if links else None,
        )
        set_settings(self.gate, IntegrationSettings(slack=SLACK), "admin")
        self.gate.request_approval(GOOD_RULE_ID, 1, proposer)
        self.queue = queue_for(self.storage, clock=self.clock)
        dispatch(self.gate, self.queue)
        worker = Worker(
            self.queue,
            JobContext(database=self.path),
            handlers=make_handlers(self.runtime, self.clock),
            isolation="thread",
        )
        while worker.run_once() is not None:
            pass
        keys = ApiKeyService(
            self.storage, params=ScryptParams(n=1024), permissions_for=key_permissions
        )
        from scruple.integrations import approvals_api

        real = approvals_api.approvals_router
        self.now = NOW
        self._patch = lambda gates, root: real(gates, root, self.runtime, lambda: self.now)
        approvals_api.approvals_router = self._patch  # type: ignore[assignment]
        try:
            self.client = TestClient(create_app(self.gate, auth=Authenticator(keys)))
        finally:
            approvals_api.approvals_router = real

    def dm(self, user: str) -> dict[str, Any]:
        return next(p for p in self.slack.posts if p["channel"] == user)

    def link(self, user: str, action: str) -> str:
        actions = self.dm(user)["blocks"][-1]["elements"]
        url = next(e["url"] for e in actions if e["action_id"].endswith(action))
        return str(urlsplit(url).path)

    def click(
        self,
        user: str,
        action: str = APPROVE_ACTION,
        *,
        value: str | None = None,
        secret: str = SIGNING,
        ts: int | None = None,
        team: str = "T0001",
    ) -> Any:
        channel = next(p for p in self.slack.posts if p["channel"] == "C0001")
        buttons = channel["blocks"][-1]["elements"]
        v = value or next(b["value"] for b in buttons if b["action_id"] == action)
        payload = {
            "type": "block_actions",
            "team": {"id": team},
            "user": {"id": user},
            "actions": [{"action_id": action, "value": v}],
            "response_url": "https://hooks.slack.com/actions/T0001/1/abc",
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = str(ts if ts is not None else int(self.now))
        headers = {
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": slack_signature(secret, stamp, body),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self.client.post("/integrations/slack/interactions", content=body, headers=headers)

    def kinds(self, prefix: str = "integration.") -> list[str]:
        return [e.event_type for e in self.gate.audit.entries() if e.event_type.startswith(prefix)]


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


# --- outgoing -------------------------------------------------------------------------


def test_request_posts_block_kit_to_the_channel_and_links_to_eligible_approvers(w: World) -> None:
    channels = [p["channel"] for p in w.slack.posts]
    assert channels == ["C0001", "UALICE", "UBOB"]  # proposer and unnamed dave get no links
    msg = w.slack.posts[0]
    assert msg["text"].startswith("Approval requested: " + GOOD_RULE_ID)
    kinds = [b["type"] for b in msg["blocks"]]
    assert kinds == ["header", "section", "context", "actions"]
    fields = " ".join(f["text"] for f in msg["blocks"][1]["fields"])
    assert PROPOSER in fields and f"`{GOOD_RULE_ID}` v1" in fields
    assert "synthetic" in msg["blocks"][2]["elements"][0]["text"]
    approve, reject = msg["blocks"][3]["elements"]
    assert approve["action_id"] == APPROVE_ACTION and approve["style"] == "primary"
    assert reject["action_id"] == REJECT_ACTION and "confirm" in reject
    value = json.loads(approve["value"])
    assert value["t"] == "default" and value["r"] == GOOD_RULE_ID and value["v"] == 1
    assert "url" not in json.dumps(msg["blocks"][3])  # no links in the shared channel
    dm = w.dm("UALICE")["blocks"][-1]["elements"]
    assert all(
        e["url"].startswith("https://scruple.example.com/integrations/approval-links/v1.k1.")
        for e in dm
    )
    # the job is idempotent: running notify again sends nothing new
    (event,) = [e for e in IntegrationStore(w.storage).events() if e.type == "approval.requested"]
    assert notify(w.storage, w.runtime, event.id, clock=w.clock)["sent"] == []
    assert len(w.slack.posts) == 3


def test_post_message_errors_and_partial_retries(tmp_path: Path) -> None:
    w = World(tmp_path)
    (event,) = [e for e in IntegrationStore(w.storage).events() if e.type == "approval.requested"]
    store = IntegrationStore(w.storage)
    # link_nonces is append-only; drop the guard to simulate markers that were never written
    w.storage.connection.execute("DROP TRIGGER link_nonces_no_delete")
    w.storage.connection.execute("DELETE FROM link_nonces")
    w.slack.posts.clear()
    w.slack.answers = [{"ok": True}, {"ok": False, "error": "ratelimited"}]
    with pytest.raises(TransientSlackError, match="ratelimited"):
        notify(w.storage, w.runtime, event.id, clock=w.clock)
    assert store.nonce_used(f"sent:{event.id}:C0001")
    out = notify(w.storage, w.runtime, event.id, clock=w.clock)
    assert out["sent"] == ["UALICE", "UBOB"]  # the channel message is not repeated
    w.storage.connection.execute("DROP TRIGGER IF EXISTS link_nonces_no_delete")
    w.storage.connection.execute("DELETE FROM link_nonces")
    w.slack.answers = [{"ok": False, "error": "invalid_auth"}]
    with pytest.raises(PermanentJobError, match="invalid_auth"):
        notify(w.storage, w.runtime, event.id, clock=w.clock)
    w.slack.answers = [503]
    with pytest.raises(TransientSlackError):
        notify(w.storage, w.runtime, event.id, clock=w.clock)
    with pytest.raises(PermanentJobError):
        notify(w.storage, w.runtime, "evt_missing", clock=w.clock)
    bare = Runtime(env={}, http=w.runtime.http)
    with pytest.raises(PermanentJobError, match="not set"):
        notify(w.storage, bare, event.id, clock=w.clock)


def test_without_link_keys_no_links_are_sent(tmp_path: Path) -> None:
    w = World(tmp_path, links=False)
    assert [p["channel"] for p in w.slack.posts] == ["C0001"]
    job = next(j for j in w.queue.list_jobs() if j.kind == "slack_notify")
    assert job.result is not None and job.result["links_skipped"] == [ALICE.name, BOB.name]


def test_non_interactive_messages_have_no_buttons() -> None:
    from scruple.integrations.store import GateEvent

    cfg = SLACK.model_copy(update={"interactive": False})
    e = GateEvent(
        id="evt_1",
        type="approval.requested",
        tenant="default",
        created_at=T0,
        audit_seq=1,
        audit_entry_hash="h",
        data={"rule_id": "r", "version": 1, "content_hash": "c" * 64, "request_seq": 1},
    )
    assert [b["type"] for b in approval_message(cfg, e)["blocks"]] == [
        "header",
        "section",
        "context",
    ]


# --- interactive buttons -------------------------------------------------------------------


def test_two_approvals_from_slack_activate(w: World) -> None:
    r = w.click("UALICE")
    assert r.status_code == 200 and "1 more approval" in r.json()["text"]
    r = w.click("UBOB")
    assert "activated by decision" in r.json()["text"]
    assert w.gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE
    granted = [e for e in w.gate.audit.entries() if e.event_type == "approval.granted"]
    assert [e.actor for e in granted] == [ALICE.name, BOB.name]
    actions = [e for e in w.gate.audit.entries() if e.event_type == "integration.approval_action"]
    assert [a.payload["slack_user"] for a in actions] == ["UALICE", "UBOB"]
    assert all(a.payload["via"] == "slack" for a in actions)
    assert [x["response_type"] for x in w.slack.responses] == ["ephemeral", "ephemeral"]
    assert w.gate.verify_integrity().ok


def test_reject_from_slack(w: World) -> None:
    r = w.click("UALICE", REJECT_ACTION)
    assert r.json()["text"].startswith("rejected; decision")
    assert w.gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.REJECTED
    rej = next(e for e in w.gate.audit.entries() if e.event_type == "approval.rejected")
    assert rej.actor == ALICE.name and "Slack" in rej.payload["reason"]


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"secret": "wrong"}, 401),
        ({"ts": int(NOW) - 301}, 401),
        ({"ts": int(NOW) + 301}, 401),
        ({"team": "T9999"}, 401),
        ({"value": json.dumps({"t": "no-such-tenant", "r": "x", "v": 1, "q": 1, "h": "h"})}, 401),
    ],
)
def test_unverified_slack_requests_are_refused(w: World, kw: dict[str, Any], code: int) -> None:
    assert w.click("UALICE", **kw).status_code == code
    assert "approval.granted" not in w.kinds("approval.")
    assert w.gate.storage.for_tenant("no-such-tenant").list_rule_versions() == []


@pytest.mark.parametrize(
    ("user", "needle"),
    [
        ("UNOBODY", "not mapped"),
        ("UPROP", "not a named approver"),  # mapped, but not in the approval policy
        ("UDAVE", "not a named approver"),
    ],
)
def test_mapped_identity_and_named_approvers(w: World, user: str, needle: str) -> None:
    r = w.click(user)
    assert (
        r.status_code == 200
        and r.json()["text"].startswith("Not done")
        and needle in r.json()["text"]
    )
    assert "integration.approval_refused" in w.kinds()
    assert "approval.granted" not in w.kinds("approval.")


def test_separation_of_duties_holds_from_slack(tmp_path: Path) -> None:
    w = World(tmp_path, proposer=ALICE.name)  # alice requested it, so alice cannot approve
    r = w.click("UALICE")
    assert "separation of duties" in r.json()["text"]
    refused = [e for e in w.gate.audit.entries() if e.event_type == "integration.approval_refused"]
    assert refused and "separation of duties" in refused[-1].payload["reason"]


def test_a_button_press_cannot_be_replayed(w: World) -> None:
    first = w.click("UALICE")
    assert "1 more approval" in first.json()["text"]
    again = w.click("UALICE")  # identical body and signature
    assert "already used" in again.json()["text"]


def test_stale_buttons_and_foreign_actions(w: World) -> None:
    channel = w.slack.posts[0]["blocks"][-1]["elements"][0]["value"]
    stale = json.dumps({**json.loads(channel), "q": 1})
    assert "no longer open" in w.click("UALICE", value=stale).json()["text"]
    changed = json.dumps({**json.loads(channel), "h": "0" * 64})
    assert "rule changed" in w.click("UALICE", value=changed).json()["text"]
    r = w.client.post(
        "/integrations/slack/interactions",
        content=urlencode({"payload": json.dumps({"actions": [{"action_id": "other"}]})}).encode(),
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert w.client.post("/integrations/slack/interactions", content=b"x=1").status_code == 400
    big = b"payload=" + b"a" * 70_000
    assert w.client.post("/integrations/slack/interactions", content=big).status_code == 413


# --- signed links -----------------------------------------------------------------------------


def test_link_page_changes_nothing_and_the_post_acts_once(w: World) -> None:
    path = w.link("UALICE", "approve")
    for _ in range(2):  # previews and scanners may fetch it
        page = w.client.get(path)
        assert page.status_code == 200 and "Nothing has happened yet" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "approval.granted" not in w.kinds("approval.")
    assert w.client.post(path, content=b"").status_code == 400
    done = w.client.post(path, content=b"confirm=yes")
    assert done.status_code == 200 and "1 more approval" in done.text
    assert w.client.post(path, content=b"confirm=yes").status_code == 409
    assert w.client.get(path).status_code == 410
    action = [e for e in w.gate.audit.entries() if e.event_type == "integration.approval_action"]
    assert action[0].payload["via"] == "approval_link" and action[0].payload["link_key"] == "k1"
    # bob's link completes the quorum
    assert "activated" in w.client.post(w.link("UBOB", "approve"), content=b"confirm=yes").text
    assert w.gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE


def test_reject_link_needs_a_reason(w: World) -> None:
    path = w.link("UALICE", "reject")
    assert "Reason (required)" in w.client.get(path).text
    assert "needs a reason" in w.client.post(path, content=b"confirm=yes").text
    r = w.client.post(path, content=urlencode({"confirm": "yes", "reason": "too risky"}).encode())
    assert r.status_code == 200 and "rejected" in r.text
    rej = next(e for e in w.gate.audit.entries() if e.event_type == "approval.rejected")
    assert rej.payload["reason"] == "too risky" and rej.actor == ALICE.name


def test_tampered_expired_and_revoked_links(w: World) -> None:
    path = w.link("UALICE", "approve")
    token = unquote(path.rsplit("/", 1)[1])
    head, kid, _body, sig = token.split(".")
    bob = unquote(w.link("UBOB", "approve").rsplit("/", 1)[1]).split(".")
    swapped = ".".join([head, kid, bob[2], sig])  # bob's claims, alice's signature
    assert w.client.get(f"/integrations/approval-links/{swapped}").status_code == 410
    assert (
        w.client.post(f"/integrations/approval-links/{swapped}", content=b"confirm=yes").status_code
        == 409
    )
    assert w.client.get("/integrations/approval-links/garbage").status_code == 410
    # revoked: bob removed from the Slack mapping
    without_bob = SLACK.model_copy(update={"approvers": SLACK.approvers[:1]})
    set_settings(w.gate, IntegrationSettings(slack=without_bob), "admin")
    r = w.client.post(w.link("UBOB", "approve"), content=b"confirm=yes")
    assert r.status_code == 409 and "no longer a mapped approver" in r.text
    # expired: a day later
    w.now = NOW + 24 * 3600 + 1
    r = w.client.get(path)
    assert r.status_code == 410 and "expired" in r.text
    assert "approval.granted" not in w.kinds("approval.")


def test_link_expiry_never_outlives_the_request(w: World) -> None:
    assert isinstance(w.runtime.links, LinkSigner)
    token = unquote(w.link("UALICE", "approve").rsplit("/", 1)[1])
    _, claims = w.runtime.links.verify(token)
    req = [e for e in w.gate.audit.entries() if e.event_type == "approval.requested"][-1]
    expires = datetime.fromisoformat(req.payload["expires_at"]).timestamp()
    assert claims["e"] <= expires and claims["e"] == int(NOW) + SLACK.link_ttl_seconds
    assert claims["s"] == ALICE.name and claims["a"] == "approve" and len(claims["n"]) == 32


def test_links_with_another_tenants_key_ring_are_refused(w: World) -> None:
    path = w.link("UALICE", "approve")
    w.runtime.links = LinkSigner({"k9": b"\x09" * 32}, "k9")  # k1 rotated out
    assert "no longer accepted" in w.client.get(path).text


# --- configuration --------------------------------------------------------------------------


def test_slack_config_rules() -> None:
    with pytest.raises(ValidationError, match="public_base_url"):
        SLACK.model_validate({**SLACK.model_dump(), "public_base_url": None})
    dup = {**SLACK.model_dump(), "approvers": [SLACK.approvers[0].model_dump()] * 2}
    with pytest.raises(ValidationError, match="once"):
        SlackConfig.model_validate(dup)
    with pytest.raises(ValidationError):
        SlackConfig.model_validate({**SLACK.model_dump(), "signing_secret_envs": ["NOT_OURS"]})
    with pytest.raises(ValidationError):
        SlackConfig.model_validate({**SLACK.model_dump(), "channel": "#general"})
    assert SLACK.approver_for("UALICE") is not None and SLACK.approver_for("UX") is None


def test_audit_chain_survives_every_path(w: World) -> None:
    w.click("UNOBODY")
    w.click("UALICE")
    w.client.post(w.link("UALICE", "approve"), content=b"confirm=yes")  # duplicate grant
    w.client.post(w.link("UBOB", "approve"), content=b"confirm=yes")
    assert w.gate.verify_integrity().ok
    kinds = w.kinds()
    assert kinds.count("integration.approval_action") == 2
    assert kinds.count("integration.approval_refused") == 2
    assert re.search(r"approval\.satisfied", " ".join(w.kinds("approval.")))
    assert w.clock.t - T0 < timedelta(seconds=1)
