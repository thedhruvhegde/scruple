"""The hosted read-only demo (`scruple serve --demo`, scruple/demo/).

Most tests seed a small synthetic tenant (the baseline policy and a `tenant.json`
that marks it synthetic) so they run in a second; the slow one serves the real
`scruple console demo-seed` data.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.deps import iter_routes, protection_of
from scruple.auth.models import Permission
from scruple.cli.main import main
from scruple.console.demo_seed import SeedReport
from scruple.demo import BANNER
from scruple.demo.guard import CLOSED, READ_ONLY
from scruple.demo.host import DemoHost, next_reset
from scruple.service import AgentGate
from scruple.settings import Settings, SettingsError, load_settings
from scruple.storage import open_storage

WRITES = ("POST", "PUT", "PATCH", "DELETE")
CONSOLE_PAGES = (
    "/console/dashboard",
    "/console/dashboard/savings",
    "/console/dashboard/payments",
    "/console/dashboard/routing",
    "/console/dashboard/compute",
    "/console/rules",
    "/console/runs",
    "/console/diff/agents",
    "/console/diff/rules",
    "/console/approvals",
    "/console/backtests",
    "/console/rules/base-fx-contest/versions/1",
    "/console/agents/evidence",
)


def small_seed(db: str, data: Path, tenant: str, *, say: Callable[[str], None]) -> SeedReport:
    """A tenant with the baseline policy only, marked synthetic like the real seed."""
    storage: Any = open_storage(db).for_tenant(tenant)
    AgentGate(storage).bootstrap_baseline("risk-ops@demo.example")
    storage.close()
    folder = data / tenant
    folder.mkdir(parents=True)
    (folder / "tenant.json").write_text(json.dumps({"synthetic": True, "label": "test"}))
    say("seeded")
    return SeedReport(tenant, db, folder)


def _settings(**over: Any) -> Settings:
    return load_settings(env={}, **over)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[DemoHost]:
    # Limits high enough for the route walks (one test checks the real limits).
    s = _settings(rate_limit_anonymous_per_minute=100_000, rate_limit_anonymous_burst=100_000)
    h = DemoHost(tmp_path / "demo", s, seed=small_seed, retire_after_s=0.0)
    h.wipe()
    h.reset()
    yield h
    h.stop()
    if h.current is not None:
        h.current.gate.storage.close()


@pytest.fixture
def client(host: DemoHost) -> TestClient:
    return TestClient(host)


def _fill(path: str) -> str:
    values = {
        "rule_id": "base-fx-contest",
        "version": "1",
        "decision_id": "dec-00001",
        "job_id": "job-00000001",
        "name": "evidence",
        "episode_id": "ep-000000000000",
        "filter_id": "flt-000000000000",
        "endpoint_id": "whe_0000000000000000",
        "delivery_id": "whd_000000000000000000000000",
        "token": "x",
        "provider": "stripe",
        "source": "stripe",
    }
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: values.get(m.group(1), "x"), path)


def _reads_only(route: Any) -> bool:
    """True when the route needs nothing beyond `read` (or is public)."""
    for req in protection_of(route):
        if req.kind == "signed":
            return False
        if req.kind == "permission" and req.permission is not Permission.READ:
            return False
    return True


# --- writes: every registered route, through the host and through the app alone ---------


def test_no_write_route_is_reachable(host: DemoHost, client: TestClient) -> None:
    assert host.current is not None
    app = host.current.app
    bare = TestClient(app)  # the app alone: demo_guard must refuse without DemoHost
    routes = list(iter_routes(app))
    writes = [(m, r.path) for r in routes for m in sorted(r.methods or ()) if m in WRITES]
    # The routes that matter most are among those walked.
    paths = {p for _m, p in writes}
    for must in (
        "/rules",
        "/rules/{rule_id}/versions/{version}/approve",
        "/rules/{rule_id}/rollback",
        "/console/approvals/{rule_id}/{version}/approve",
        "/jobs",
    ):
        assert must in paths, must
    assert any(p.startswith("/ingest") or "webhook" in p for p in paths)
    assert len(writes) >= 20
    for method, path in writes:
        url = _fill(path)
        for c in (client, bare):
            r = c.request(method, url, json={"x": 1})
            assert r.status_code == 403, (method, path, r.status_code)
            assert r.json()["detail"] == READ_ONLY, (method, path)
    # and any other method or path, routed or not
    for method in (*WRITES, "OPTIONS"):
        r = client.request(method, "/no/such/route")
        assert r.status_code == 403 and r.json()["detail"] == READ_ONLY


def test_get_routes_beyond_read_are_closed(host: DemoHost, client: TestClient) -> None:
    assert host.current is not None
    closed = 0
    for route in iter_routes(host.current.app):
        if "GET" not in (route.methods or ()):
            continue
        r = client.get(_fill(route.path), follow_redirects=False)
        if _reads_only(route):
            assert r.status_code != 401, route.path
            assert not (r.status_code == 403 and CLOSED in r.text), route.path
        else:
            closed += 1
            assert r.status_code == 403, (route.path, r.status_code)
            assert r.json()["detail"] == CLOSED, route.path
    assert closed >= 3  # metrics, key listing, usage events, signed approval links


# --- reads ---------------------------------------------------------------------------


def test_console_pages_carry_the_banner_and_no_forms_that_write(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/console/"
    for page in CONSOLE_PAGES:
        r = client.get(page)
        assert r.status_code == 200, page
        assert f'<div class="band band-sand demo-strip" role="note"><p class="page">{BANNER}' in (
            r.text
        ), page
        assert 'method="post"' not in r.text, page
        assert "<script" not in r.text, page
        assert "script-src 'none'" in r.headers["content-security-policy"], page
    missing = client.get("/console/rules/nope/versions/9")
    assert missing.status_code == 404 and BANNER in missing.text


def test_visitor_is_read_only_and_credentials_are_ignored(client: TestClient) -> None:
    me = client.get("/auth/whoami").json()
    assert me["tenant"] == "demo" and me["permissions"] == ["read"] and me["method"] == "demo"
    for headers in ({"X-API-Key": "scr_junk"}, {"Authorization": "Basic x"}, {"X-Actor": "a"}):
        r = client.get("/rules", headers=headers)
        assert r.status_code == 200 and r.json(), headers
    assert client.get("/health").status_code == 200


def test_no_demo_banner_outside_demo_mode(tmp_path: Path) -> None:
    from scruple.auth.authn import Authenticator

    storage: Any = open_storage(str(tmp_path / "g.db"))
    gate = AgentGate(storage)
    gate.bootstrap_baseline()
    app = create_app(
        gate, _settings(auth_dev_actor_header=True), auth=Authenticator(dev_actor_header=True)
    )
    r = TestClient(app).get("/console/rules", headers={"X-Actor": "someone"})
    assert r.status_code == 200 and BANNER not in r.text
    storage.close()


# --- resource safety -----------------------------------------------------------------


def test_rate_limit_counts_each_client_address(tmp_path: Path) -> None:
    s = _settings(
        demo_client_ip_header="Fly-Client-IP",
        rate_limit_anonymous_per_minute=1,
        rate_limit_anonymous_burst=2,
    )
    h = DemoHost(tmp_path / "demo", s, seed=small_seed, retire_after_s=0.0)
    h.reset()
    c = TestClient(h)
    one = {"Fly-Client-IP": "203.0.113.7"}
    assert [c.get("/rules", headers=one).status_code for _ in range(3)] == [200, 200, 429]
    assert c.get("/rules", headers={"Fly-Client-IP": "203.0.113.8"}).status_code == 200
    assert c.get("/health", headers=one).status_code == 200  # probes are never limited
    assert h.current is not None
    h.current.gate.storage.close()


def test_request_bodies_are_capped(tmp_path: Path) -> None:
    h = DemoHost(tmp_path / "demo", _settings(max_body_bytes=1024), seed=small_seed)
    h.reset()
    r = TestClient(h).request("GET", "/rules", content=b"x" * 4096)
    assert r.status_code == 413
    assert h.current is not None
    h.current.gate.storage.close()


def test_unseeded_host_answers_503(tmp_path: Path) -> None:
    h = DemoHost(tmp_path / "demo", _settings(), seed=small_seed)
    r = TestClient(h).get("/console/")
    assert r.status_code == 503 and r.headers["retry-after"] == "60"


def test_reset_swaps_in_a_new_generation_and_retires_the_old(host: DemoHost) -> None:
    first = host.current
    assert first is not None and first.folder.exists()
    second = host.reset()
    assert host.current is second and second.folder != first.folder
    deadline = time.monotonic() + 10
    while first.folder.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not first.folder.exists() and second.folder.exists()
    assert TestClient(host).get("/rules").status_code == 200


def test_restart_wipes_earlier_generations(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    (root / "gen-old").mkdir(parents=True)
    (root / "keep.txt").write_text("not ours")
    h = DemoHost(root, _settings(), seed=small_seed)
    h.wipe()
    assert not (root / "gen-old").exists() and (root / "keep.txt").exists()


def test_failed_reset_keeps_the_old_generation(host: DemoHost) -> None:
    before = host.current

    def broken(*_a: Any, **_k: Any) -> SeedReport:
        raise RuntimeError("seed failed")

    host.seed = broken
    with pytest.raises(RuntimeError):
        host.reset()
    assert host.current is before


def test_next_reset_is_the_next_hour_mark() -> None:
    at = datetime(2026, 11, 12, 3, 59, tzinfo=UTC)
    assert next_reset(at, 4) == datetime(2026, 11, 12, 4, tzinfo=UTC)
    assert next_reset(datetime(2026, 11, 12, 4, tzinfo=UTC), 4) == datetime(
        2026, 11, 13, 4, tzinfo=UTC
    )


# --- starting it ---------------------------------------------------------------------


def test_demo_setting_refuses_any_other_server(tmp_path: Path) -> None:
    storage: Any = open_storage(str(tmp_path / "g.db"))
    with pytest.raises(SettingsError, match="scruple serve --demo"):
        create_app(AgentGate(storage), _settings(demo=True))
    storage.close()


def test_serve_demo_refuses_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="--db does not apply"):
        main(["serve", "--demo", "--db", "x.db"])


@pytest.mark.slow
def test_real_seed_serves_every_console_page(tmp_path: Path) -> None:
    h = DemoHost(tmp_path / "demo", _settings())
    h.reset()
    c = TestClient(h)
    for page in CONSOLE_PAGES:
        r = c.get(page)
        assert r.status_code == 200 and BANNER in r.text, page
    cp = c.get("/console/dashboard/compute").text
    assert "demo-worker-1" in cp
    assert c.post("/rules/inr-delivered-contest/rollback", json={"reason": "x"}).status_code == 403
    assert h.current is not None
    h.current.gate.storage.close()
