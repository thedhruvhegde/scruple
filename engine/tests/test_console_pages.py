"""Console pages (roadmap 181-183, backtests, evidence downloads): every route answers
with its key content, the markup has the same structure everywhere, and every route
declares a permission. Synthetic data only."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.ci.evidence import load_pack
from scruple.console.permissions import declared_permission
from scruple.console.routes import STATIC, console_router
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE_ID, GOOD_RULE_ID
from tests.test_console_support import (
    CANARY_SEED,
    DEV_AUTH,
    EVAL_SEED,
    READER,
    SECRET,
    client,
    ready_gate,
    sources,
    structure,
    write_report,
)


@pytest.fixture(scope="module")
def gate() -> AgentGate:
    return ready_gate()


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("reports")
    write_report(root)
    return root


@pytest.fixture(scope="module")
def web(gate: AgentGate, reports: Path) -> TestClient:
    return client(gate, sources(reports))


@pytest.fixture(scope="module")
def episode(web: TestClient) -> str:
    src = sources()
    return src.agents["evidence"].records[0].episode_id


def _pages(episode: str) -> list[tuple[str, tuple[str, ...]]]:
    return [
        ("/console/rules", ("Runs and scores", f"{GOOD_RULE_ID}@v1", "evidence", "always-accept")),
        ("/console/agents/evidence", ("Agent version evidence", episode, "Failed checks")),
        (
            f"/console/agents/evidence/episodes/{episode}",
            ("Stage transitions", "Journal entries", "Content sha256", "Tool calls", "Every check"),
        ),
        (
            f"/console/rules/{GOOD_RULE_ID}/versions/1",
            ("Shadow replay", "Canary", "dec-00001", "Evidence pack (JSON)", "rule.registered"),
        ),
        (f"/console/rules/{BAD_RULE_ID}/versions/1", ("block", "Checks")),
        ("/console/diff/agents", ("Where the versions differ", "Cases with a different outcome")),
        (
            f"/console/diff/rules?a={BAD_RULE_ID}@v1&b={GOOD_RULE_ID}@v1",
            ("Where the versions differ", "replay set seed", "first match wins"),
        ),
        ("/console/approvals", ("Approvals inbox", f"{GOOD_RULE_ID}@v1", "Approve", "Reject")),
        ("/console/backtests", ("contest-3ds", "synthetic")),
        (
            "/console/backtests/contest-3ds",
            ("Synthetic data.", "Per dispute reason", "Regression e"),
        ),
    ]


def test_every_page_renders_with_its_key_content(web: TestClient, episode: str) -> None:
    for path, needles in _pages(episode):
        r = web.get(path)
        assert r.status_code == 200, (path, r.text[:500])
        assert r.headers["content-type"].startswith("text/html")
        for n in needles:
            assert n in r.text, (path, n)


def test_pages_share_one_structure(web: TestClient, episode: str) -> None:
    """Snapshot-free: one h1, lang, title, no inline script, no inline styles or event
    handler attributes (CSP forbids them anyway), CSRF on every POST form, unique ids,
    and every link stays inside the console or on the page."""
    for path, _ in _pages(episode):
        html = web.get(path).text
        s = structure(html)
        assert html.startswith("<!doctype html>"), path
        assert s.h1 == 1, path
        assert s.inline_scripts == 0, path
        assert ("html", {"lang": "en"}) in s.tags, path
        ids = [i for _, a in s.tags if (i := a.get("id"))]
        assert len(ids) == len(set(ids)), (path, sorted(i for i in ids if ids.count(i) > 1))
        for tag, attrs in s.tags:
            assert "style" not in attrs, (path, tag)
            assert not any(k.startswith("on") for k in attrs), (path, tag, attrs)
            href = attrs.get("href") or attrs.get("action") or attrs.get("src")
            if href is not None and tag in ("a", "form", "link", "script"):
                assert href.startswith(("/console/", "#")), (path, href)
        for form in s.forms:
            if form["method"] == "post":
                assert form["csrf"], path


def test_security_headers_on_every_page(web: TestClient, episode: str) -> None:
    for path, _ in _pages(episode):
        r = web.get(path)
        assert "<script" not in r.text, path
        h = r.headers
        csp = h["content-security-policy"]
        assert "script-src 'none'" in csp and "default-src 'none'" in csp
        assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["cache-control"] == "no-store"


def test_no_secrets_on_any_page(web: TestClient, episode: str) -> None:
    for path, _ in _pages(episode):
        text = web.get(path).text
        for secret in (str(EVAL_SEED), str(CANARY_SEED), f"{EVAL_SEED:,}", SECRET.decode()):
            assert secret not in text, (path, secret)


def test_version_page_shows_evalue_statistics() -> None:
    g = ready_gate(require_canary=False, stats="evalue")
    rec = next(r for r in g.decisions(GOOD_RULE_ID) if r.decision.shadow is not None)
    assert rec.decision.shadow is not None and rec.decision.shadow.evalue is not None
    ev = rec.decision.shadow.evalue
    r = client(g).get(f"/console/rules/{GOOD_RULE_ID}/versions/1")
    assert r.status_code == 200
    for needle in ("E-values per case type", "Required n", "Regression e", "Confidence sequence"):
        assert needle in r.text
    for t in ev.types:
        assert html.escape(t.case_type, quote=False) in r.text  # '<' is escaped
        if t.required_n is not None:
            assert f"{t.required_n:,}" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/console/agents/nobody",
        "/console/agents/evidence/episodes/ep-000000000000",
        "/console/rules/no-such-rule/versions/1",
        f"/console/rules/{GOOD_RULE_ID}/versions/9",
        "/console/backtests/nothing",
        "/console/diff/agents?a=evidence&b=nobody",
        "/console/diff/rules?a=nope@v1&b=nope@v2",
        "/console/decisions/dec-99999/report.md",
        f"/console/rules/{GOOD_RULE_ID}/versions/9/evidence.json",
    ],
)
def test_unknown_things_are_404_pages(web: TestClient, path: str) -> None:
    r = web.get(path)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Nothing was changed" in r.text


@pytest.mark.parametrize(
    "path",
    ["/console/agents/..%2F..%2Fetc", "/console/agents/UPPER", "/console/rules/x/versions/0"],
)
def test_malformed_path_parameters_are_rejected(web: TestClient, path: str) -> None:
    assert web.get(path).status_code in (404, 422)


def test_unknown_query_parameter_is_422(web: TestClient) -> None:
    assert web.get("/console/approvals", params={"sneaky": "1"}).status_code == 422
    assert web.get("/console/approvals", params={"msg": "<b>hi</b>"}).status_code == 422


def test_root_redirects_to_console(web: TestClient) -> None:
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/console/"


@pytest.mark.parametrize("name", sorted(STATIC))
def test_static_files_are_served(web: TestClient, name: str) -> None:
    r = web.get(f"/console/static/{name}")
    assert r.status_code == 200 and len(r.content) > 100
    assert r.headers["content-type"].split(";")[0] == STATIC[name].split(";")[0]


@pytest.mark.parametrize(
    "name",
    ["../routes.py", "templates/base.html", "console.css.map", "%2e%2e/routes.py", "console.js"],
)
def test_only_whitelisted_static_files(web: TestClient, name: str) -> None:
    assert web.get(f"/console/static/{name}").status_code == 404


def test_stylesheet_follows_the_design_rules(web: TestClient) -> None:
    import re

    css = re.sub(r"/\*.*?\*/", "", web.get("/console/static/console.css").text, flags=re.S).lower()
    for banned in ("gradient", "box-shadow", "border-radius: 1", "inter,", "space grotesk"):
        assert banned not in css, banned
    assert "italic" not in css
    assert '"martian mono"' in css and '"ibm plex sans condensed"' in css
    assert "prefers-color-scheme: dark" in css and 'data-theme="dark"' in css
    assert "fonts.googleapis" not in css and "http" not in css.replace("https", "")


def test_evidence_pack_downloads(web: TestClient, gate: AgentGate) -> None:
    r = web.get(f"/console/rules/{GOOD_RULE_ID}/versions/1/evidence.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    pack, claimed = load_pack(r.text)
    assert claimed == pack.pack_sha256 and pack.rule_id == GOOD_RULE_ID
    assert pack.data == "synthetic"
    pdf = web.get(f"/console/rules/{GOOD_RULE_ID}/versions/1/evidence.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    md = web.get("/console/decisions/dec-00001/report.md")
    assert md.status_code == 200 and "dec-00001" in md.text
    bt = web.get("/console/backtests/contest-3ds/report.json")
    assert bt.status_code == 200 and json.loads(bt.text)["synthetic"] is True


def test_version_without_decisions_has_no_pack(gate: AgentGate) -> None:
    from scruple.registry import Provenance
    from scruple.sim.library import GOOD_RULE

    spec = GOOD_RULE.model_copy(update={"priority": 77, "description": "not yet evaluated"})
    gate.propose("fresh-rule", spec, "ops-team", Provenance(agent_id="a-1", episode_ids=("e",)))
    c = client(gate)
    page = c.get("/console/rules/fresh-rule/versions/1")
    assert page.status_code == 200 and "Evidence pack" not in page.text
    assert c.get("/console/rules/fresh-rule/versions/1/evidence.json").status_code == 404


def test_agent_diff_pairs_the_same_cases(web: TestClient) -> None:
    src = sources()
    a, b = src.agents["evidence"], src.agents["always-accept"]
    r = web.get("/console/diff/agents", params={"a": "evidence", "b": "always-accept"})
    assert r.status_code == 200
    assert f"<b>{len(a.records)}</b> paired episodes" in r.text
    changed = [
        x.episode_id
        for x, y in zip(a.records, b.records, strict=True)
        if x.resolution != y.resolution or x.cost_usd_minor != y.cost_usd_minor
    ]
    for ep in changed:
        assert ep in r.text
    if changed:
        side = web.get(
            "/console/diff/agents",
            params={"a": "evidence", "b": "always-accept", "case": changed[0]},
        )
        assert "SIDE BY SIDE" in side.text
        assert side.text.count('class="screen side"') == 2


def test_rule_diff_counts_match_a_direct_replay(web: TestClient, gate: AgentGate) -> None:
    from scruple.console.views import rule_diff

    a = gate.registry.get(BAD_RULE_ID, 1)
    b = gate.registry.get(GOOD_RULE_ID, 1)
    d = rule_diff(gate, a, b, 11, 300)
    assert sum(t.n for t in d.types) == 300
    assert sum(t.changed for t in d.types) == len(d.changed)
    assert d.total_b - d.total_a == sum(c.delta for c in d.changed)
    r = web.get("/console/diff/rules", params={"a": a.ref, "b": b.ref})
    assert f"outcome changed <b>{len(d.changed)}</b>" in r.text
    if d.changed:
        pick = d.changed[0].key
        ta, tb = d.trace(pick, 0), d.trace(pick, 1)
        assert ta is not None and tb is not None
        assert sum(s.chosen for s in ta.steps) <= 1 and sum(s.chosen for s in tb.steps) <= 1
        assert ta.result.action != tb.result.action or ta.result.resolution != tb.result.resolution
    assert d.trace("no-such-case", 0) is None


def test_every_console_route_declares_a_permission(gate: AgentGate) -> None:
    router = console_router(gate, sources(), secret=SECRET)
    seen: dict[str, str] = {}
    for route in router.routes:
        assert isinstance(route, APIRoute)
        perms = [
            p
            for d in route.dependant.dependencies
            if d.call is not None and (p := declared_permission(d.call)) is not None
        ]
        if route.path.startswith("/console/static/"):
            assert perms == [], "static files are public"
            continue
        assert len(perms) == 1, route.path
        seen[f"{sorted(route.methods or ())[0]} {route.path}"] = perms[0]
    # Every POST that changes the gate needs approve; the tenant's shared saved
    # filters are tenant configuration, so curating them needs admin.
    filters = {"POST /console/filters", "POST /console/filters/{filter_id}/delete"}
    assert all(p == "approve" for k, p in seen.items() if k.startswith("POST") and k not in filters)
    assert {seen[k] for k in filters} == {"admin"}
    downloads = [k for k in seen if k.endswith((".json", ".pdf", ".md"))]
    assert downloads and all(seen[k] == "export" for k in downloads)
    reads = [k for k in seen if k.startswith("GET") and k not in downloads]
    assert reads and all(seen[k] == "read" for k in reads)


def test_mounted_under_the_api_and_hidden_from_the_schema(gate: AgentGate) -> None:
    api = TestClient(create_app(gate, auth=DEV_AUTH), headers={"X-Actor": READER})
    assert api.get("/console/").status_code == 200
    assert api.get("/console/approvals").status_code == 200
    schema = api.get("/openapi.json").json()
    assert not any(p.startswith("/console") for p in schema["paths"])
    assert api.get("/rules").status_code == 200  # the JSON API is unchanged


def test_x_actor_header_is_shown_and_malformed_one_refused(web: TestClient) -> None:
    r = web.get("/console/", headers={"X-Actor": "alice@demo.bank"})
    assert "acting as alice@demo.bank" in r.text
    assert web.get("/console/", headers={"X-Actor": "bad actor <x>"}).status_code == 401
