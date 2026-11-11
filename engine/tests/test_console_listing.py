"""Console search, filters, pagination and saved filters (D06): server-side, escaped,
tenant-scoped. Synthetic data only."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from markupsafe import escape

from scruple.console.listing import (
    CASES,
    DEFAULT_PER,
    MAX_Q,
    RUNS,
    VERSIONS,
    Filter,
    FilterError,
    matches,
    paginate,
)
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE_ID, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from scruple.storage.console_filters import (
    MAX_PER_LIST,
    SavedFilterError,
    SQLSavedFilters,
    filter_id,
)
from tests.test_console_support import (
    IMG,
    SCRIPT,
    client,
    csrf_token,
    ready_gate,
    sources,
    structure,
)

XSS = [SCRIPT, IMG, '"><svg onload=alert(1)>', "' onmouseover='x", "</textarea><b>x"]


@pytest.fixture(scope="module")
def gate() -> AgentGate:
    return ready_gate()


@pytest.fixture
def web(gate: AgentGate) -> TestClient:
    return client(gate, sources())


# --- the listing core -------------------------------------------------------------------


def test_paginate_filters_then_pages() -> None:
    rows = [{"n": i, "kind": "even" if i % 2 == 0 else "odd"} for i in range(53)]
    f = Filter.build(VERSIONS, None, {"status": "even"}, page=2, per=10)
    pg = paginate(rows, f, lambda r: str(r["n"]), lambda r: {"status": str(r["kind"])})
    assert pg.total == 53 and pg.matched == 27 and pg.pages == 3
    assert [r["n"] for r in pg.items] == list(range(20, 40, 2))
    assert (pg.first, pg.last) == (11, 20)
    assert pg.options["status"] == ("even", "odd")


def test_page_past_the_end_is_clamped() -> None:
    rows = list(range(5))
    pg = paginate(rows, Filter.build(RUNS, None, {}, page=99), str, lambda _r: {})
    assert pg.page == 1 and pg.items == rows


@given(
    n=st.integers(0, 300),
    per=st.sampled_from([10, 25, 50, 100]),
    page=st.integers(1, 40),
)
@settings(max_examples=60)
def test_pages_partition_the_matches(n: int, per: int, page: int) -> None:
    rows = list(range(n))
    f = Filter.build(RUNS, None, {}, page=1, per=per)
    pg = paginate(rows, f, str, lambda _r: {})
    seen: list[int] = []
    for p in range(1, pg.pages + 1):
        seen += paginate(
            rows, Filter.build(RUNS, None, {}, page=p, per=per), str, lambda _r: {}
        ).items
    assert seen == rows
    one = paginate(rows, Filter.build(RUNS, None, {}, page=page, per=per), str, lambda _r: {})
    assert len(one.items) <= per and 1 <= one.page <= max(1, pg.pages)


@pytest.mark.parametrize(
    ("text", "q", "hit"),
    [
        ("dec-00001 shadow promote", "SHADOW", True),
        ("dec-00001 shadow promote", "promote dec-00001", True),
        ("dec-00001 shadow promote", "promote canary", False),
        ("anything", "", True),
    ],
)
def test_search_is_all_terms_case_insensitive(text: str, q: str, hit: bool) -> None:
    assert matches(text, q) is hit


def test_filter_validation() -> None:
    with pytest.raises(FilterError):
        Filter.build(VERSIONS, "x" * (MAX_Q + 1), {})
    with pytest.raises(FilterError):
        Filter.build(VERSIONS, None, {"nope": "x"})
    with pytest.raises(FilterError):
        Filter.build(VERSIONS, None, {"status": "<b>"})
    with pytest.raises(FilterError):
        Filter.build(VERSIONS, None, {}, per=7)
    assert Filter.build(VERSIONS, "  a   b ", {}).q == "a b"


def test_canonical_round_trips_and_drops_paging() -> None:
    f = Filter.build(CASES, "fraud <x>", {"gate": "fail", "family": "friendly"}, page=3, per=50)
    assert f.canonical() == "q=fraud+%3Cx%3E&family=friendly&gate=fail"
    assert Filter.parse(CASES, f.canonical()) == Filter.build(CASES, f.q, dict(f.facets))
    assert "page=3" in f.query() and "per=50" in f.query()
    with pytest.raises(FilterError):
        Filter.parse(CASES, "q=a&q=b")
    with pytest.raises(FilterError):
        Filter.parse(CASES, "page=2&evil=1")


@given(st.text(max_size=MAX_Q))
def test_any_search_term_survives_a_round_trip(q: str) -> None:
    f = Filter.build(RUNS, q, {})
    assert Filter.parse(RUNS, f.canonical()).q == f.q


# --- pages ----------------------------------------------------------------------------


def test_runs_page_lists_every_decision_and_filters_by_stage(
    web: TestClient, gate: AgentGate
) -> None:
    r = web.get("/console/runs")
    assert r.status_code == 200
    for rec in gate.decisions():
        assert rec.decision.decision_id in r.text
    r = web.get("/console/runs?stage=canary")
    ids = set(re.findall(r">(dec-\d+)</a>", r.text))
    want = {x.decision.decision_id for x in gate.decisions() if x.decision.stage.value == "canary"}
    assert ids == want and want


def test_overview_search_and_status_filter(web: TestClient) -> None:
    def rules_table(url: str) -> str:
        return web.get(url).text.split('id="agents"')[0]

    t = rules_table(f"/console/?q={BAD_RULE_ID}")
    assert f">{BAD_RULE_ID}@v1<" in t and f">{GOOD_RULE_ID}@v1<" not in t
    t = rules_table("/console/?status=rejected")
    assert f">{BAD_RULE_ID}@v1<" in t and f">{GOOD_RULE_ID}@v1<" not in t
    assert "No rule versions match this filter." in rules_table("/console/?status=retired")


def test_cases_page_pages_and_filters(web: TestClient) -> None:
    run = sources().agents["evidence"]
    r = web.get("/console/agents/evidence?per=10")
    assert r.status_code == 200
    first = run.records[0].episode_id
    assert first in r.text
    r = web.get("/console/agents/evidence?gate=fail")
    cards = run.scorecards
    failing = {e for e, c in cards.items() if not c.gate}
    shown = set(re.findall(r">(ep-[0-9a-f]{12})</a>", r.text))
    assert shown == failing


def test_approvals_history_filter_by_event(web: TestClient) -> None:
    r = web.get("/console/approvals?event=rule.activated")
    assert r.status_code == 200
    assert "approval.granted" not in r.text.split('id="history"')[1]


def test_pager_links_keep_the_filter(gate: AgentGate) -> None:
    g = ready_gate()
    for _ in range(9):
        g.run_assurance()
    web = client(g, sources())
    total = len(g.decisions())
    assert total > 10
    r = web.get("/console/runs?q=dec&per=10")
    assert 'href="/console/runs?q=dec&amp;per=10&amp;page=2"' in r.text
    r2 = web.get("/console/runs?q=dec&per=10&page=2")
    assert 'aria-current="page">2<' in r2.text


@pytest.mark.parametrize(
    "path",
    [
        "/console/?q={x}",
        "/console/runs?q={x}",
        "/console/agents/evidence?q={x}",
        "/console/approvals?q={x}",
    ],
)
@pytest.mark.parametrize("term", XSS)
def test_search_terms_are_escaped(web: TestClient, path: str, term: str) -> None:
    from urllib.parse import quote

    r = web.get(path.format(x=quote(term)))
    assert r.status_code == 200, r.text[:300]
    assert term not in r.text
    assert str(escape(term)) in r.text
    s = structure(r.text)
    assert s.inline_scripts == 0
    for tag, attrs in s.tags:
        assert not any(k.startswith("on") for k in attrs), (tag, attrs)
        assert tag not in ("svg", "img") or "onload" not in attrs


@pytest.mark.parametrize(
    "query",
    ["status=%3Cb%3E", "per=7", "page=0", "nope=1", "q=" + "x" * 101],
)
def test_bad_filter_inputs_are_refused(web: TestClient, query: str) -> None:
    r = web.get(f"/console/?{query}")
    assert r.status_code == 422


def test_listing_pages_share_the_console_structure(web: TestClient) -> None:
    for path in ("/console/runs", "/console/?q=x", "/console/agents/evidence?gate=fail"):
        h = web.get(path)
        s = structure(h.text)
        assert s.h1 == 1 and s.inline_scripts == 0
        for tag, attrs in s.tags:
            href = attrs.get("href") or attrs.get("action")
            if href is not None and tag in ("a", "form"):
                assert href.startswith(("/console/", "#")), (path, href)
        for form in s.forms:
            if form["method"] == "post":
                assert form["csrf"], path
        assert h.headers["content-security-policy"].startswith("default-src 'none'")


# --- saved filters ------------------------------------------------------------------------


def _save(web: TestClient, **form: str) -> Any:
    token = csrf_token(web, "/console/runs?stage=canary")
    return web.post("/console/filters", data={"csrf": token, **form}, follow_redirects=False)


def test_save_apply_and_delete_a_filter(gate: AgentGate) -> None:
    web = client(ready_gate(), sources())
    r = _save(web, list="runs", name="canaries", query="stage=canary")
    assert r.status_code == 303 and r.headers["location"] == "/console/runs?stage=canary"
    page = web.get("/console/runs").text
    assert 'href="/console/runs?stage=canary">canaries</a>' in page
    fid = filter_id("runs", "canaries")
    token = csrf_token(web, "/console/runs")
    r = web.post(f"/console/filters/{fid}/delete", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/console/runs"
    assert "canaries</a>" not in web.get("/console/runs").text


def test_saving_needs_csrf(web: TestClient) -> None:
    r = web.post("/console/filters", data={"list": "runs", "name": "x", "query": "q=a"})
    assert r.status_code == 403


@pytest.mark.parametrize("name", XSS)
def test_saved_filter_names_and_queries_are_escaped(name: str) -> None:
    web = client(ready_gate(), sources())
    q = "q=" + name.replace("&", "%26").replace("#", "%23")
    assert _save(web, list="runs", name=name, query=q).status_code == 303
    page = web.get("/console/runs").text
    assert name not in page
    assert str(escape(name)) in page


@pytest.mark.parametrize(
    ("form", "status"),
    [
        ({"list": "runs", "name": "x", "query": "evil=1"}, 422),
        ({"list": "runs", "name": "x", "query": ""}, 422),
        ({"list": "nope", "name": "x", "query": "q=a"}, 422),
        ({"list": "cases", "name": "x", "query": "q=a", "agent": "no-such-agent"}, 404),
        ({"list": "runs", "name": "", "query": "q=a"}, 422),
        ({"list": "runs", "name": "a\nb", "query": "q=a"}, 422),
        ({"list": "runs", "name": "x", "query": "q=a", "extra": "1"}, 422),
    ],
)
def test_bad_saves_are_refused(gate: AgentGate, form: dict[str, str], status: int) -> None:
    web = client(ready_gate(), sources())
    assert _save(web, **form).status_code == status


def test_cases_filter_returns_to_its_agent() -> None:
    web = client(ready_gate(), sources())
    r = _save(web, list="cases", name="fails", query="gate=fail", agent="evidence")
    assert r.status_code == 303 and r.headers["location"] == "/console/agents/evidence?gate=fail"
    assert "fails</a>" in web.get("/console/agents/evidence").text


def test_only_the_saver_may_delete(gate: AgentGate) -> None:
    web = client(ready_gate(), sources())
    assert _save(web, list="runs", name="mine", query="q=x").status_code == 303
    token = csrf_token(web, "/console/runs")
    fid = filter_id("runs", "mine")
    r = web.post(
        f"/console/filters/{fid}/delete",
        data={"csrf": token},
        headers={"X-Actor": "someone-else@demo.bank"},
    )
    assert r.status_code == 403
    assert "mine</a>" in web.get("/console/runs").text


def test_saved_filters_are_tenant_scoped() -> None:
    db = SQLiteStorage(":memory:")
    store = SQLSavedFilters(db.connection)
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    store.save("acme", "runs", "a", "q=x", "u", now)
    assert [f.name for f in store.list("acme", "runs")] == ["a"]
    assert store.list("other", "runs") == []
    with pytest.raises(LookupError):
        store.delete("other", filter_id("runs", "a"), "u", now)
    # Saving the same name again replaces it (latest wins), on the record.
    store.save("acme", "runs", "a", "q=y", "u", now)
    assert [f.query for f in store.list("acme", "runs")] == ["q=y"]
    store.delete("acme", filter_id("runs", "a"), "u", now)
    assert store.list("acme", "runs") == []
    rows = db.connection.query("SELECT action FROM console_filter_events ORDER BY id")
    assert [r["action"] for r in rows] == ["save", "save", "delete"]
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("UPDATE console_filter_events SET name='z'")
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("DELETE FROM console_filter_events")
    with pytest.raises(SavedFilterError):
        store.save("acme", "nope", "a", "q=x", "u", now)
    for i in range(MAX_PER_LIST):
        store.save("acme", "cases", f"f{i}", "q=x", "u", now)
    with pytest.raises(SavedFilterError):
        store.save("acme", "cases", "one-more", "q=x", "u", now)
    store.save("acme", "cases", "f0", "q=z", "u", now)  # replacing one is still fine
    db.close()


def test_default_page_size(web: TestClient) -> None:
    run = sources().agents["evidence"]
    r = web.get("/console/agents/evidence")
    shown = re.findall(r">(ep-[0-9a-f]{12})</a>", r.text)
    assert len(shown) == min(DEFAULT_PER, len(run.records))


def test_saving_filters_needs_admin_and_readers_see_no_save_form() -> None:
    from scruple.auth.authn import Authenticator
    from scruple.auth.keys import ApiKeyService, ScryptParams
    from scruple.auth.rbac import key_permissions
    from scruple.console.app import create_console_app
    from tests.test_console_support import SECRET

    g = ready_gate()
    keys = ApiKeyService(g.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)
    _, auditor = keys.create("examiner", (), "local-admin", roles=["auditor"])
    _, admin = keys.create("risk@demo.bank", (), "local-admin", roles=["risk_admin"])
    app = create_console_app(g, sources(), secret=SECRET, auth=Authenticator(keys))
    reader = TestClient(app, headers={"X-API-Key": auditor.get_secret_value()})
    page = reader.get("/console/runs?stage=canary").text
    assert "Save filter" not in page
    token = csrf_token(reader, "/console/runs")
    r = reader.post(
        "/console/filters", data={"csrf": token, "list": "runs", "name": "x", "query": "q=a"}
    )
    assert r.status_code == 403
    boss = TestClient(app, headers={"X-API-Key": admin.get_secret_value()})
    assert "Save filter" in boss.get("/console/runs?stage=canary").text
    token = csrf_token(boss, "/console/runs")
    r = boss.post(
        "/console/filters",
        data={"csrf": token, "list": "runs", "name": "x", "query": "q=a"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "x</a>" in reader.get("/console/runs").text  # shared with the tenant's readers
