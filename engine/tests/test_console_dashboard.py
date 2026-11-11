"""The purchaser's dashboard (/console/dashboard and its four pages).

Two tenants share one database and one data root. Tenant A gets something in every
source a panel reads (decisions, policy, jobs, workers, usage, billing pushes,
imported shadow summaries, a case store, an eval run, an anchor, a marker), each
carrying a distinctive string and planted markup. Tenant B must see none of it on
any page, A must see all of it escaped, and every page keeps the console's CSP and
structure rules. Synthetic data only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scruple.api.strict import reject_unknown_inputs
from scruple.audit.anchor import GitAnchor, statement_for
from scruple.auth import guard_route
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import Limit
from scruple.auth.rbac import Role, key_permissions
from scruple.backtest import BacktestConfig, load_stripe, rule_candidate, run_backtest
from scruple.billing.push import BillingLedger, PlannedPush
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.clock import SteppingClock
from scruple.console.dashboard import window
from scruple.console.routes import console_router
from scruple.console.security import CSP
from scruple.console.sources import ConsoleSources
from scruple.console.tenant_files import tenant_files
from scruple.domain import Action
from scruple.gate.config import GateConfig
from scruple.ingest.models import CaseState, ExternalCase
from scruple.ingest.store import CaseStore, StoredCase
from scruple.ingest.stripe.mapping import ingest_dispute
from scruple.jobs.queue import queue_for
from scruple.policy.apply import apply_policy
from scruple.policy.models import Policy
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, GOOD_RULE
from scruple.sim.models import Stage
from scruple.storage import SQLiteStorage
from scruple.storage.imported_shadow import imported_shadows
from scruple.usage.meter import Meter, usage_store_for
from tests.test_console_support import IMG, SCRIPT, structure

A, B = "bank-a", "bank-b"
SECRET = b"dashboard-test-secret-0123456789abcdef"
FAST = ScryptParams(n=1024)
MARK = "zq7tenantaonly"  # in every identifier tenant A owns
RAW = (SCRIPT, IMG, "<script>", "<img src=x")
PAGES = (
    "/console/dashboard",
    "/console/dashboard/savings",
    "/console/dashboard/payments",
    "/console/dashboard/routing",
    "/console/dashboard/compute",
)
CFG = GateConfig(
    n_cases=800, bootstrap_resamples=100, canary_n_cases=1_000, approvers=("ops@a.example",)
)


def _gate(db: SQLiteStorage, tenant: str) -> AgentGate:
    return AgentGate(db.for_tenant(tenant), SteppingClock(datetime(2026, 9, 1, tzinfo=UTC)), CFG)


def _backtest_body(dataset: str) -> dict[str, object]:
    history = load_stripe(json.dumps(stripe_disputes(DEFAULT_PROFILE, 200, 3)), label=dataset)
    cand = rule_candidate(
        {
            "name": f"{MARK}-candidate",
            "rules": [
                {
                    "description": "contest 3-D Secure disputes",
                    "conditions": [{"field": "three_ds", "op": "eq", "value": True}],
                    "action": "contest",
                    "priority": 10,
                }
            ],
            "default_action": "accept",
        }
    )
    rep = run_backtest(history, cand, config=BacktestConfig(n_synthetic=300))
    return rep.model_dump(mode="json")


def _fill_tenant_a(db: SQLiteStorage, root: Path) -> AgentGate:
    g = _gate(db, A)
    g.bootstrap_baseline()
    apply_policy(
        g,
        Policy.model_validate({"name": f"{MARK}-policy", "description": f"{SCRIPT} {IMG}"}),
        "ops@a.example",
    )
    prov = Provenance(agent_id=f"{MARK}-agent")
    g.propose(f"{MARK}-good", GOOD_RULE, "ops-a", prov)
    g.evaluate(f"{MARK}-good", 1)
    g.propose(f"{MARK}-bad", BAD_RULE, "ops-a", prov)
    g.evaluate(f"{MARK}-bad", 1)
    # jobs: a succeeded backtest (markup in its dataset label), a failed job (markup in
    # its error), all claimed by A's own worker
    q = queue_for(db)
    job, _ = q.submit_raw("backtest", {"rule_id": f"{MARK}-good", "version": 1}, tenant_id=A)
    claimed = q.claim(f"{MARK}-worker", 60)
    assert claimed is not None and claimed.job_id == job.job_id
    q.succeed(claimed, f"{MARK}-worker", {"report": _backtest_body(f"{MARK} {SCRIPT}")})
    q.submit_raw("custom_kind", {"x": 1}, tenant_id=A, max_attempts=1)
    bad = q.claim(f"{MARK}-worker", 60)
    assert bad is not None
    q.fail(bad, f"{MARK}-worker", f"boom {SCRIPT} {IMG}", retry=False)
    # usage and a billing push
    usage = usage_store_for(db, g.clock)
    assert usage is not None and usage.sync(g) > 0
    BillingLedger(db.connection).record(
        PlannedPush(
            tenant=A,
            meter=Meter.GATE_RUN,
            day=date(2026, 9, 1),
            delta=2,
            cumulative=2,
            event_name=f"{MARK}_meter",
            customer="cus_aaaaaaaa",
            identifier="scr-a",
            timestamp=0,
        ),
        False,
        datetime(2026, 9, 2, tzinfo=UTC),
    )
    # imported shadow on A's candidate
    rows = [ingest_dispute(o) for o in stripe_disputes(DEFAULT_PROFILE, 12, 4)]
    from scruple.gate.shadow_imported import ImportedDecision

    decisions = [
        ImportedDecision(
            case=b.cases[0],
            action=Action.ACCEPT,
            decided_at=b.cases[0].opened_at + timedelta(hours=1),
        )
        for b in rows
    ]
    imported_shadows(g, A)(f"{MARK}-good", 1).record(decisions)
    # A's data folder
    folder = root / A
    folder.mkdir(parents=True)
    (folder / "tenant.json").write_text(
        json.dumps({"synthetic": True, "label": f"{MARK} label {SCRIPT}"})
    )
    with CaseStore(folder / "cases.db") as store:
        case = ExternalCase(
            source="stripe",
            external_id=f"dp_{MARK}",
            payment_ref="ch_1",
            stage=Stage.CHARGEBACK,
            state=CaseState.OPEN,
            reason_code=None,
            source_reason=f"{MARK}<script>alert(1)</script>",
            amount_minor=1234,
            currency="USD",
            opened_at=datetime(2026, 9, 1, tzinfo=UTC),
            evidence_due_at=datetime(2026, 9, 3, tzinfo=UTC),
            evidence_fields=(IMG,),
        )
        store.write_run(
            source="stripe",
            input_sha256="0" * 64,
            label="test",
            cases=[StoredCase(case=case)],
            movements=[],
            errors=[],
            report={},
            synthetic=True,
        )
    run = folder / "evals" / f"{MARK}-agent" / "run-1"
    (run / "runs").mkdir(parents=True)
    manifest = {
        "provider": "anthropic",
        "model": f"{MARK}-model {SCRIPT}",
        "strategy": "react",
        "path": "api",
        "transport": "fake",
        "seeds": "0",
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    record = {
        "system": "x",
        "provider": "anthropic",
        "model": "m",
        "strategy": "react",
        "path": "api",
        "real_model": False,
        "seed": 0,
        "episode_id": "ep-000000000001",
        "stop": "submitted",
        "input_tokens": 777_001,
        "output_tokens": 5,
        "cost_usd": 0.25,
        "score": {"reward": 0.5},
        "row": {},
    }
    (run / "runs" / "seed-0.json").write_text(json.dumps(record))
    st_ = statement_for(g.audit.entries(), A)
    anchor = GitAnchor(statement=st_, repo="r", path="p", commit="c" * 40, committed_at=IMG)
    (folder / "anchors").mkdir()
    (folder / "anchors" / "a.json").write_text(anchor.model_dump_json())
    # an anchor file that names B, kept in A's folder: never shown to anyone as B's
    other = GitAnchor(
        statement=st_.model_copy(update={"tenant": B}),
        repo="r",
        path="p",
        commit="d" * 40,
        committed_at="x",
    )
    (folder / "anchors" / "b.json").write_text(other.model_dump_json())
    return g


@dataclass
class World:
    db: SQLiteStorage
    root: Path
    clients: dict[str, TestClient]


def _clients(db: SQLiteStorage, gates: dict[str, AgentGate], root: Path) -> dict[str, TestClient]:
    """The console as served, with real API keys: one auditor (read only) per tenant."""
    from scruple.auth.middleware import AuthMiddleware

    keys = ApiKeyService(db, params=FAST, permissions_for=key_permissions)
    many = Limit(per_minute=1_000_000, burst=1_000_000)
    app = FastAPI(dependencies=[Depends(guard_route), Depends(reject_unknown_inputs)])
    app.add_middleware(
        AuthMiddleware,
        authenticator=Authenticator(keys),
        limits=Limits(default=many, anonymous=many, auth_failures=many),
    )
    src = ConsoleSources(agents={}, replay_seed=5, replay_cases=200, data=root)
    app.include_router(console_router(lambda t: gates[t], src, secret=SECRET))
    out = {}
    for t in gates:
        _, tok = keys.create(f"auditor-{t}", [], "admin", tenant=t, roles=[Role.AUDITOR])
        out[t] = TestClient(app, headers={"X-API-Key": tok.get_secret_value()})
    return out


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    root = tmp_path_factory.mktemp("data")
    db = SQLiteStorage(":memory:")
    _fill_tenant_a(db, root)
    _gate(db, B).bootstrap_baseline()
    gates = {t: _gate(db, t) for t in (A, B)}
    yield World(db, root, _clients(db, gates, root))
    db.close()


def _body(html: str) -> str:
    return html.split("<body>", 1)[1]


# --- pages ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("period", ["30d", "90d", "365d", "all"])
def test_every_page_renders_for_every_period(world: World, path: str, period: str) -> None:
    for t in (A, B):
        r = world.clients[t].get(path, params={"period": period})
        assert r.status_code == 200, (t, path, r.text[:300])
        assert r.headers["content-security-policy"] == CSP
        s = structure(r.text)
        assert s.h1 == 1 and s.inline_scripts == 0
        for tag, attrs in s.tags:
            assert "style" not in attrs, (tag, attrs)  # the CSP refuses inline style
            assert not any(k.startswith("on") for k in attrs), (tag, attrs)
            href = attrs.get("href") or attrs.get("src") or attrs.get("action")
            if href is not None and tag in ("a", "form", "script", "link"):
                assert href.startswith(("/console/", "#")), (path, href)
        assert s.forms == []  # read-only: no form on any dashboard page


def test_console_opens_on_the_dashboard(world: World) -> None:
    r = world.clients[A].get("/console/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/console/dashboard"
    assert TestClient(world.clients[A].app).get("/console/dashboard").status_code == 401


@pytest.mark.parametrize("query", ["period=7d", "period=<b>", "nope=1"])
def test_bad_period_or_unknown_parameter_is_refused(world: World, query: str) -> None:
    assert world.clients[A].get(f"/console/dashboard?{query}").status_code == 422


def test_dashboard_pages_need_only_read(world: World) -> None:
    from fastapi.routing import APIRoute

    from scruple.console.permissions import declared_permission

    router = console_router(lambda t: _gate(world.db, t), ConsoleSources(agents={}), secret=SECRET)
    seen = {}
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/console/dashboard"):
            seen[route.path] = [
                p
                for d in route.dependant.dependencies
                if d.call is not None and (p := declared_permission(d.call)) is not None
            ]
    assert set(seen) == set(PAGES) and all(p == ["read"] for p in seen.values())


# --- tenant isolation: every panel ------------------------------------------------------------


def test_tenant_a_sees_its_own_data_in_every_panel(world: World) -> None:
    c = world.clients[A]
    pages = {p: c.get(p).text for p in PAGES}
    sav, pay, rt, cp = (pages[p] for p in PAGES[1:])
    assert f"{MARK}-bad@v1" in sav and "SYNTHETIC" in sav and f"{MARK}-good@v1" in sav
    assert f"{MARK} &lt;script&gt;" in sav  # the backtest job's report, its dataset label
    assert f"dp_{MARK}" in pay and f"{MARK}_meter" in pay  # due soon, and the push
    assert f"{MARK}-policy" in rt and f"{MARK}-agent" in rt
    assert f"{MARK}-worker" in cp and "777,001" in cp and "c" * 12 in cp


def test_tenant_b_sees_nothing_of_tenant_a(world: World) -> None:
    c = world.clients[B]
    for p in PAGES:
        for period in ("30d", "all"):
            text = c.get(p, params={"period": period}).text
            assert MARK not in text, (p, period)
            assert "777,001" not in text and "dddddddddddd" not in text
    cp = c.get("/console/dashboard/compute").text
    assert "No jobs in this period." in cp and "none on record" in cp
    pay = c.get("/console/dashboard/payments").text
    assert "No case store for this tenant." in pay and "Nothing pushed" in pay
    assert 'class="banner"' not in c.get("/console/dashboard").text  # A's marker is A's


def test_another_tenants_anchor_in_a_folder_is_never_used(world: World) -> None:
    files = tenant_files(world.root, A)
    assert files is not None
    assert {a.statement.tenant for a in files.anchors()} == {A}


@pytest.mark.parametrize("tenant", ["../bank-a", "bank-a/..", "BANK-A", "", "a", ".."])
def test_tenant_folder_never_escapes_the_root(world: World, tenant: str) -> None:
    assert tenant_files(world.root, tenant) is None
    assert tenant_files(None, A) is None


# --- escaping ----------------------------------------------------------------------------------


def test_planted_markup_is_escaped_everywhere(world: World) -> None:
    for p in PAGES:
        text = world.clients[A].get(p).text
        body = _body(text)
        for raw in RAW:
            assert raw not in body, (p, raw)
        assert not re.findall(r"<script\b", text), p
    pay = world.clients[A].get("/console/dashboard/payments").text
    assert f"{MARK}&lt;script&gt;" in pay  # the reason code, in the table and the chart
    cp = world.clients[A].get("/console/dashboard/compute").text
    assert "boom &lt;script&gt;" in cp


def test_period_window() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    w = window("30d", now)
    assert w.has(now - timedelta(days=29)) and not w.has(now - timedelta(days=31))
    assert window("all", now).has(datetime(2000, 1, 1, tzinfo=UTC))


# --- the demo seed ------------------------------------------------------------------------------


def test_demo_seed_refuses_a_tenant_with_history(tmp_path: Path) -> None:
    from scruple.console.demo_seed import seed_demo

    db = tmp_path / "gate.db"
    s = SQLiteStorage(str(db))
    AgentGate(s.for_tenant("demo"), SteppingClock()).bootstrap_baseline()
    s.close()
    with pytest.raises(ValueError, match="already has history"):
        seed_demo(str(db), tmp_path / "data", "demo", say=lambda _m: None)


@pytest.mark.slow
def test_demo_seed_fills_every_panel_with_labelled_synthetic_data(tmp_path: Path) -> None:
    from scruple.console.demo_seed import seed_demo

    db = str(tmp_path / "gate.db")
    rep = seed_demo(db, tmp_path / "data", "demo", say=lambda _m: None)
    assert rep.jobs["succeeded"] >= 4 and rep.jobs["failed"] == 1 and rep.jobs["queued"] == 1
    assert rep.disputes > 0 and rep.imported > 0 and rep.pushes > 0 and rep.eval_runs == 2
    s = SQLiteStorage(db)
    g = AgentGate(s.for_tenant("demo"))
    assert g.verify_integrity().ok
    web = _clients(s, {"demo": g}, tmp_path / "data")["demo"]
    sav, pay, rt, cp = (web.get(p).text for p in PAGES[1:])
    for text in (sav, pay, rt, cp):
        assert 'class="banner"><b>SYNTHETIC</b>' in text
    assert "inr-delivered-contest-fast@v1" in sav and "No change was blocked" not in sav
    assert "synthetic" in pay and "No case store" not in pay and "[" in pay
    assert "demo-disputes" in rt and "ramping" in rt and "alice@demo.example" in rt
    assert "demo-worker-1" in cp and "demo-fake-model" in cp and "anchor check" in cp
    assert "intact" in cp
    files = tenant_files(tmp_path / "data", "demo")
    marker = files.marker() if files is not None else None
    assert marker is not None and marker.synthetic
    s.close()


def test_demo_seed_command_line(tmp_path: Path) -> None:
    from scruple.cli.main import main

    with pytest.raises(SystemExit, match="--data"):
        main(["console", "demo-seed", "--db", str(tmp_path / "g.db")])
