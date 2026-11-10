"""scripts/smoke.sh against a real HTTP server (uvicorn in a thread, SQLite)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from scruple.api.app import create_app
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.storage import SQLiteStorage
from scruple.version import build_info

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "scripts" / "smoke.sh"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(settings: Settings) -> Iterator[tuple[str, AgentGate]]:
    g = AgentGate(
        SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=500, bootstrap_resamples=100)
    )
    g.bootstrap_baseline()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(g, settings=settings),
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", g
    finally:
        server.should_exit = True
        t.join(timeout=10)


@pytest.fixture
def server() -> Iterator[tuple[str, AgentGate]]:
    yield from _serve(Settings())


def _smoke(url: str, **env: str) -> subprocess.CompletedProcess[str]:
    import os

    base = {k: v for k, v in os.environ.items() if not k.startswith("SCRUPLE_SMOKE_")}
    return subprocess.run(
        [str(SMOKE), url], capture_output=True, text=True, env=base | env, check=False, timeout=120
    )


def test_read_only_smoke_passes_and_writes_nothing(server: tuple[str, AgentGate]) -> None:
    url, g = server
    before = len(g.audit.entries())
    r = _smoke(url + "/")  # trailing slash is fine
    assert r.returncode == 0, r.stdout + r.stderr
    for line in ("ok    health", "ok    ready (database: reachable", "ok    version"):
        assert line in r.stdout
    assert "metrics closed to anonymous callers (HTTP 404)" in r.stdout
    assert "skip  evaluate" in r.stdout
    assert len(g.audit.entries()) == before


def _key(g: AgentGate, subject: str, role: Role) -> str:
    keys = ApiKeyService(g.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)
    _, token = keys.create(subject, (), "local-admin", roles=[role])
    return token.get_secret_value()


def test_keyed_smoke_registers_and_evaluates(server: tuple[str, AgentGate]) -> None:
    url, g = server
    r = _smoke(url, SCRUPLE_SMOKE_API_KEY=_key(g, "smoke-bot", Role.PROPOSER))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok    whoami smoke-bot (tenant default, propose,read)" in r.stdout
    assert "ok    register smoke-" in r.stdout
    assert "ok    evaluate smoke-" in r.stdout and "shadow=" in r.stdout
    smoke_rules = [v for v in g.registry.all_versions() if v.rule_id.startswith("smoke-")]
    assert len(smoke_rules) == 1 and smoke_rules[0].owner == "smoke-bot"
    assert smoke_rules[0].provenance.agent_id == "smoke-bot"  # from the key, not a header


@pytest.mark.parametrize("bad", ["not-a-key", "scr_0123456789abcdef_" + "a" * 43])
def test_keyed_smoke_with_a_bad_key_fails_before_writing(
    server: tuple[str, AgentGate], bad: str
) -> None:
    url, g = server
    before = len(g.audit.entries())
    r = _smoke(url, SCRUPLE_SMOKE_API_KEY=bad)
    assert r.returncode == 1 and "FAIL  whoami: HTTP 401" in r.stderr
    assert "register" not in r.stdout + r.stderr
    assert len(g.audit.entries()) == before


def test_keyed_smoke_with_a_read_only_key_cannot_register(server: tuple[str, AgentGate]) -> None:
    url, g = server
    r = _smoke(url, SCRUPLE_SMOKE_API_KEY=_key(g, "auditor", Role.AUDITOR))
    assert r.returncode == 1 and "FAIL  register: HTTP 403" in r.stderr


def test_expected_commit_mismatch_fails(server: tuple[str, AgentGate]) -> None:
    url, _ = server
    assert _smoke(url, SCRUPLE_SMOKE_EXPECT_COMMIT=build_info().commit[:7]).returncode == 0
    r = _smoke(url, SCRUPLE_SMOKE_EXPECT_COMMIT="ffffffffffff0000")
    assert r.returncode == 1 and "expected commit ffffffffffff0000" in r.stderr


def test_open_metrics_are_flagged() -> None:
    for url, _ in _serve(Settings(metrics_public=True)):
        r = _smoke(url)
        assert r.returncode == 1 and "metrics answered HTTP 200 without credentials" in r.stderr
        assert _smoke(url, SCRUPLE_SMOKE_METRICS_PUBLIC="1").returncode == 0


def test_unreachable_server_fails() -> None:
    r = _smoke(f"http://127.0.0.1:{_free_port()}")
    assert r.returncode == 1 and "FAIL  health" in r.stderr


@pytest.mark.parametrize("args", [[], ["not-a-url"], ["http://a", "http://b"]])
def test_usage(args: list[str]) -> None:
    r = subprocess.run([str(SMOKE), *args], capture_output=True, text=True, check=False)
    assert r.returncode == 2 and "usage" in r.stderr
