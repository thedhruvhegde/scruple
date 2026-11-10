"""Approvals inbox end to end (roadmap 184): request, approve and reject through the
console against a real gate. Separation of duties is enforced by the service; the
console relays its refusals and writes nothing itself. Synthetic data only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scruple.ci.evidence import build_evidence_pack
from scruple.gate.approval import ApprovalError
from scruple.gate.models import Outcome, Stage
from scruple.registry import RuleStatus
from scruple.service import EVENT_REJECTED, AgentGate
from scruple.sim.library import GOOD_RULE_ID
from tests.test_console_support import (
    ALICE,
    BOB,
    CAROL,
    OPERATOR,
    POLICY,
    PROPOSER,
    client,
    csrf_token,
    ready_gate,
)

BASE = f"/console/approvals/{GOOD_RULE_ID}/1"


@pytest.fixture
def gate() -> AgentGate:
    return ready_gate()


def post(c: TestClient, action: str, **form: str) -> tuple[int, str]:
    token = csrf_token(c)
    r = c.post(f"{BASE}/{action}", data={"csrf": token, **form}, follow_redirects=False)
    return r.status_code, r.headers.get("location", "") or r.text


def status(g: AgentGate) -> RuleStatus:
    return g.registry.get(GOOD_RULE_ID, 1).status


def test_inbox_lists_only_versions_waiting_for_approval(gate: AgentGate) -> None:
    html = client(gate).get("/console/approvals").text
    assert f"{GOOD_RULE_ID}@v1" in html
    assert "inr-delivered-contest-fast@v1" not in html.split('id="history"')[0]  # blocked
    assert "base-fx-contest" not in html  # active, nothing to approve
    assert f'action="{BASE}/approve"' in html and f'action="{BASE}/reject"' in html
    assert "one approver from" in html  # no policy in force


def test_quorum_flow_with_separation_of_duties(gate: AgentGate) -> None:
    gate.set_approval_policy(POLICY, OPERATOR)
    c = client(gate)
    assert f'action="{BASE}/request"' in c.get("/console/approvals").text

    code, where = post(c, "request", actor=CAROL.name)
    assert (code, where) == (303, "/console/approvals?msg=requested")
    page = c.get(where).text
    assert "Approval requested" in page and CAROL.name in page
    assert f'action="{BASE}/request"' not in page  # a request is open now

    # the proposer cannot approve their own request: refused by the service, nothing written
    n = len(gate.audit.entries())
    code, text = post(c, "approve", actor=CAROL.name, reason="I proposed it, it is fine")
    assert code == 403 and "the proposer cannot approve" in text and "REFUSED" in text
    assert len(gate.audit.entries()) == n

    # someone outside the policy cannot approve either
    code, text = post(c, "approve", actor="mallory@demo.bank", reason="trust me")
    assert code == 403 and "not a named approver" in text

    code, where = post(c, "approve", actor=BOB.name, reason="canary costs at or below control")
    assert (code, where) == (303, "/console/approvals?msg=approved")
    assert status(gate) is RuleStatus.CANARY

    code, text = post(c, "approve", actor=BOB.name, reason="again")
    assert code == 403 and "already approved" in text

    code, where = post(c, "approve", actor=ALICE.name, reason="model risk signs off")
    assert (code, where) == (303, "/console/approvals?msg=activated")
    assert status(gate) is RuleStatus.ACTIVE
    act = [r for r in gate.decisions(GOOD_RULE_ID) if r.decision.stage is Stage.ACTIVATION]
    assert act and act[-1].decision.outcome is Outcome.ACTIVATED
    grants = [e for e in gate.audit.entries() if e.event_type == "approval.granted"]
    assert [g.payload["reason"] for g in grants] == [
        "canary costs at or below control",
        "model risk signs off",
    ]
    assert gate.verify_integrity().ok
    page = c.get("/console/approvals").text
    assert "Nothing is waiting for approval" in page
    assert "approval.satisfied" in page and "rule.activated" in page


def test_single_approver_flow_records_the_reason(gate: AgentGate) -> None:
    c = client(gate)
    code, _ = post(c, "approve", actor="ops-team", reason="I own it")
    assert code == 403  # the owner is not an approver
    code, where = post(c, "approve", actor=OPERATOR, reason="reviewed the evidence pack")
    assert (code, where) == (303, "/console/approvals?msg=activated")
    rec = [r for r in gate.decisions(GOOD_RULE_ID) if r.decision.stage is Stage.ACTIVATION][-1]
    assert "reason: reviewed the evidence pack" in rec.decision.reasons
    assert rec.decision.actor == OPERATOR
    assert gate.verify_integrity().ok


def test_request_needs_a_policy(gate: AgentGate) -> None:
    code, text = post(client(gate), "request", actor=PROPOSER)
    assert code == 403 and "no approval policy" in text


def test_reject_flow(gate: AgentGate) -> None:
    c = client(gate)
    code, text = post(c, "reject", actor="ops-team", reason="I do not like it")
    assert code == 403 and "not an authorised approver" in text
    assert status(gate) is RuleStatus.CANARY

    code, where = post(c, "reject", actor=OPERATOR, reason="holds too much on high value cases")
    assert (code, where) == (303, "/console/approvals?msg=rejected")
    assert status(gate) is RuleStatus.REJECTED
    rec = gate.decisions(GOOD_RULE_ID)[-1].decision
    assert (rec.stage, rec.outcome, rec.actor) == (Stage.ACTIVATION, Outcome.BLOCK, OPERATOR)
    assert "reason: holds too much on high value cases" in rec.reasons
    ev = [e for e in gate.audit.entries() if e.event_type == EVENT_REJECTED]
    assert len(ev) == 1 and ev[0].payload["reason"] == "holds too much on high value cases"
    assert gate.verify_integrity().ok

    # final: no second decision, and the version left the inbox
    code, text = post(c, "approve", actor=OPERATOR, reason="changed my mind")
    assert code == 403 and "has not passed canary" in text
    assert f"{GOOD_RULE_ID}@v1" not in c.get("/console/approvals").text.split('id="history"')[0]
    pack = build_evidence_pack(gate, GOOD_RULE_ID, 1)
    assert pack.status == "rejected" and pack.scorecard.verdict.value == "block"
    page = c.get(f"/console/rules/{GOOD_RULE_ID}/versions/1").text
    assert "Rejected at approval by" in page


def test_reject_under_a_policy_needs_a_named_approver(gate: AgentGate) -> None:
    gate.set_approval_policy(POLICY, OPERATOR)
    c = client(gate)
    code, text = post(c, "reject", actor=OPERATOR, reason="not on the list")
    assert code == 403 and "not a named approver" in text
    code, _ = post(c, "reject", actor=BOB.name, reason="ops lead says no")
    assert code == 303 and status(gate) is RuleStatus.REJECTED


def test_authenticated_principal_wins_over_the_form(gate: AgentGate) -> None:
    """With an identity from the (stub) auth layer, the form's name is ignored."""
    c = client(gate)
    token = csrf_token(c)
    r = c.post(
        f"{BASE}/approve",
        data={"csrf": token, "actor": OPERATOR, "reason": "spoofing the operator"},
        headers={"X-Actor": "ops-team"},
    )
    assert r.status_code == 403  # acted as ops-team (the owner), not as the form said
    assert status(gate) is RuleStatus.CANARY
    page = c.get("/console/approvals", headers={"X-Actor": OPERATOR}).text
    assert 'name="actor"' not in page  # no name field when the principal is known


def test_unknown_or_unready_versions(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    r = c.post(
        "/console/approvals/no-such-rule/1/approve",
        data={"csrf": token, "actor": OPERATOR, "reason": "ok then"},
    )
    assert r.status_code == 404
    r = c.post(
        "/console/approvals/inr-delivered-contest-fast/1/reject",
        data={"csrf": token, "actor": OPERATOR, "reason": "already blocked"},
    )
    assert r.status_code == 403  # blocked in shadow: never reached approval


def test_service_reject_rules() -> None:
    g = ready_gate()
    with pytest.raises(ApprovalError, match="reason"):
        g.reject_approval(GOOD_RULE_ID, 1, OPERATOR, "   ")
    with pytest.raises(ApprovalError):
        g.reject_approval(GOOD_RULE_ID, 1, "someone@else", "no")
    n = len(g.audit.entries())
    assert status(g) is RuleStatus.CANARY and len(g.audit.entries()) == n
    rec = g.reject_approval(GOOD_RULE_ID, 1, OPERATOR, "no")
    assert rec.decision.outcome is Outcome.BLOCK
    tail = [e.event_type for e in g.audit.entries()[-3:]]
    assert tail == ["decision.recorded", EVENT_REJECTED, "rule.status_changed"]
