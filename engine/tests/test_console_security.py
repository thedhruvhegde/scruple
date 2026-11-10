"""Console security: output escaping (XSS), CSRF on every POST, headers, form limits.

Markup is planted in every free-text field a console page prints: a rule's
description, an agent's explanation, a tool name and tool arguments in the trace, an
approval reason and a backtest dataset name. None may reach the page unescaped.
Synthetic data only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.console import fmt
from scruple.console.routes import environment
from scruple.console.security import COOKIE, MAX_FORM_BYTES, Csrf
from scruple.registry import Provenance, RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from tests.test_console_support import (
    IMG,
    OPERATOR,
    POLICY,
    PROPOSER,
    SCRIPT,
    client,
    csrf_token,
    ready_gate,
    sources,
    write_report,
    xss_run,
)

RAW = (SCRIPT, IMG, "<script>", "<img src=x")
APPROVE = f"/console/approvals/{GOOD_RULE_ID}/1/approve"
REJECT = f"/console/approvals/{GOOD_RULE_ID}/1/reject"


def assert_escaped(text: str) -> None:
    body = text.split("<body>", 1)[1]
    for raw in RAW:
        assert raw not in body, raw
    assert len(re.findall(r"<script\b", text)) == 1  # only the console's own script tag


@pytest.fixture
def gate() -> AgentGate:
    return ready_gate()


# --- XSS -----------------------------------------------------------------------------------


def test_agent_explanation_tool_names_and_args_are_escaped(gate: AgentGate) -> None:
    run = xss_run()
    c = client(gate, sources(extra={run.name: run}))
    ep = run.records[0]
    assert SCRIPT in ep.report.explanation
    assert any(e.tool == SCRIPT for e in ep.trace)  # the refused call is in the trace
    for path in (
        "/console/",
        f"/console/agents/{run.name}",
        f"/console/agents/{run.name}/episodes/{ep.episode_id}",
        f"/console/diff/agents?a=evidence&b={run.name}&case={ep.episode_id}",
    ):
        r = c.get(path)
        assert r.status_code == 200, path
        assert_escaped(r.text)
    page = c.get(f"/console/agents/{run.name}/episodes/{ep.episode_id}").text
    assert "I won. &lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in page


def test_rule_description_is_escaped(gate: AgentGate) -> None:
    spec = GOOD_RULE.model_copy(update={"description": f"{SCRIPT} {IMG}", "priority": 51})
    gate.propose("xss-rule", spec, "ops-team", Provenance(agent_id="a-1", episode_ids=("e",)))
    c = client(gate)
    for path in (
        "/console/rules/xss-rule/versions/1",
        "/console/diff/rules?a=xss-rule@v1&b=xss-rule@v1",
    ):
        r = c.get(path)
        assert r.status_code == 200, path
        assert_escaped(r.text)
    assert "&lt;script&gt;" in c.get("/console/rules/xss-rule/versions/1").text


def test_approval_reason_is_escaped(gate: AgentGate) -> None:
    gate.set_approval_policy(POLICY, OPERATOR)
    gate.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    c = client(gate)
    token = csrf_token(c)
    r = c.post(APPROVE, data={"csrf": token, "actor": "bob@demo.bank", "reason": SCRIPT})
    assert r.status_code == 200 and "Approval recorded" in r.text  # followed the redirect
    assert_escaped(r.text)
    assert "reason=&lt;script&gt;" in r.text
    assert_escaped(c.get(f"/console/rules/{GOOD_RULE_ID}/versions/1").text)


def test_backtest_dataset_name_is_escaped(gate: AgentGate, tmp_path: Path) -> None:
    write_report(tmp_path, dataset=f"{SCRIPT} history")
    c = client(gate, sources(tmp_path))
    for path in ("/console/backtests", "/console/backtests/contest-3ds"):
        r = c.get(path)
        assert r.status_code == 200
        assert_escaped(r.text)


def test_refused_reason_with_markup_is_escaped(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    r = c.post(APPROVE, data={"csrf": token, SCRIPT: "x", "reason": "fine by me"})
    assert r.status_code == 422  # unknown field; the message must not echo markup
    assert_escaped(r.text)


@given(st.text(max_size=80))
def test_templates_autoescape_any_text(text: str) -> None:
    env = environment()
    out = env.from_string("<p>{{ x }}</p><p title='{{ x }}'>{{ x | short(80) }}</p>").render(x=text)
    inner = out.removeprefix("<p>").removesuffix("</p>")
    assert "<" not in inner.replace("</p><p title='", "").replace("'>", "")


@given(st.floats(allow_nan=True, allow_infinity=True) | st.integers(-(10**15), 10**15))
def test_number_filters_never_raise(x: float) -> None:
    for f in (fmt.usd, fmt.signed_usd, fmt.num, fmt.evalue):
        assert isinstance(f(x), str)


# --- CSRF ----------------------------------------------------------------------------------


def _form(token: str, **kw: str) -> dict[str, str]:
    return {"csrf": token, "actor": "model-risk@demo.bank", "reason": "looks right", **kw}


def _untouched(gate: AgentGate, entries: int) -> None:
    assert gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.CANARY
    assert len(gate.audit.entries()) == entries


@pytest.mark.parametrize("path", [APPROVE, REJECT, f"/console/approvals/{GOOD_RULE_ID}/1/request"])
def test_post_without_token_is_refused(gate: AgentGate, path: str) -> None:
    c = client(gate)
    csrf_token(c)  # has a cookie, but sends no token
    n = len(gate.audit.entries())
    r = c.post(path, data={"actor": OPERATOR, "reason": "looks right"})
    assert r.status_code == 403 and "CSRF" in r.text
    _untouched(gate, n)


def test_post_without_cookie_is_refused(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    c.cookies.clear()
    n = len(gate.audit.entries())
    assert c.post(APPROVE, data=_form(token)).status_code == 403
    _untouched(gate, n)


def test_token_from_another_browser_is_refused(gate: AgentGate) -> None:
    mine, theirs = client(gate), client(gate)
    their_token = csrf_token(theirs)
    csrf_token(mine)
    n = len(gate.audit.entries())
    r = mine.post(APPROVE, data=_form(their_token))
    assert r.status_code == 403 and "bad CSRF token" in r.text
    _untouched(gate, n)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://testserver.evil"])
def test_cross_origin_post_is_refused(gate: AgentGate, origin: str) -> None:
    c = client(gate)
    token = csrf_token(c)
    n = len(gate.audit.entries())
    r = c.post(APPROVE, data=_form(token), headers={"Origin": origin})
    assert r.status_code == 403
    _untouched(gate, n)


def test_same_origin_post_with_token_is_accepted(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    r = c.post(
        APPROVE, data=_form(token), headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/console/approvals?msg=activated"
    assert gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE


def test_csrf_cookie_flags(gate: AgentGate) -> None:
    r = client(gate).get("/console/")
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{COOKIE}=")
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/console" in cookie


def test_token_is_bound_to_the_secret() -> None:
    a, b = Csrf(b"a" * 32), Csrf(b"b" * 32)
    assert a.token_for("sid") != b.token_for("sid")
    assert a.token_for("sid") != a.token_for("other")
    with pytest.raises(ValueError, match="short"):
        Csrf(b"x")


# --- form handling -------------------------------------------------------------------------


def test_json_body_is_refused(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    r = c.post(APPROVE, json=_form(token))
    assert r.status_code == 415


def test_oversized_form_is_refused(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    r = c.post(APPROVE, data=_form(token, reason="x" * (MAX_FORM_BYTES + 10)))
    assert r.status_code == 413


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({"reason": "no"}, 422),  # too short
        ({"reason": "y" * 501}, 422),
        ({"approved": "true"}, 422),  # unknown field
        ({"actor": "x y"}, 401),  # a malformed identity fails authentication
    ],
)
def test_invalid_forms_change_nothing(gate: AgentGate, extra: dict[str, str], status: int) -> None:
    c = client(gate)
    token = csrf_token(c)
    n = len(gate.audit.entries())
    r = c.post(APPROVE, data=_form(token, **extra))
    assert r.status_code == status
    _untouched(gate, n)


def test_repeated_field_is_refused(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    body = f"csrf={token}&reason=fine+by+me&reason=again&actor={OPERATOR}"
    r = c.post(APPROVE, content=body, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422


def test_anonymous_post_is_refused(gate: AgentGate) -> None:
    c = client(gate)
    token = csrf_token(c)
    del c.headers["X-Actor"]
    n = len(gate.audit.entries())
    r = c.post(APPROVE, data={"csrf": token, "reason": "looks right"})
    assert r.status_code == 401
    assert c.get("/console/approvals").status_code == 401
    _untouched(gate, n)


def test_get_on_post_route_is_not_allowed(gate: AgentGate) -> None:
    assert client(gate).get(APPROVE).status_code == 405


def test_static_files_also_carry_headers(gate: AgentGate) -> None:
    c: TestClient = client(gate)
    h = c.get("/console/static/console.js").headers
    assert h["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in h
