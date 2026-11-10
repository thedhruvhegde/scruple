"""Prometheus metrics: access control, latency by route template, gate decision
counters, the audit chain gauge, and aggregation across gunicorn workers."""

from __future__ import annotations

import functools
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from prometheus_client import REGISTRY

from scruple.api.app import create_app
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.registry.models import RULE_ID_PATTERN
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

ROOT = Path(__file__).resolve().parent.parent
AGENT = {"X-Actor": "dispute-agent-01"}
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own
# The API is exercised with the development-only X-Actor trust; /metrics checks its own
# scrape token whatever the auth mode (tests/test_auth_*.py cover real authentication).
DEV = {"auth_dev_actor_header": True}
LATENCY = "scruple_http_request_duration_seconds_count"


def _gate() -> AgentGate:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=1_500, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    return g


def _client(**settings_kw: object) -> TestClient:
    settings = Settings.model_validate({**DEV, **settings_kw})
    return TestClient(create_app(_gate(), settings=settings), headers=READER)


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_are_off_by_default() -> None:
    assert _client().get("/metrics").status_code == 404


def test_metrics_public_when_configured() -> None:
    r = _client(metrics_public=True).get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "scruple_build_info{" in r.text


@pytest.mark.parametrize(
    ("header", "status"),
    [
        (None, 401),
        ("Bearer wrong-token", 401),
        ("Basic s3cret-token-value", 401),
        ("Bearer s3cret-token-value", 200),
        ("bearer s3cret-token-value", 200),
    ],
)
def test_metrics_token(header: str | None, status: int) -> None:
    client = _client(metrics_token="s3cret-token-value", metrics_public=True)
    r = client.get("/metrics", headers={"Authorization": header} if header else {})
    assert r.status_code == status
    if status == 401:
        assert r.headers["www-authenticate"] == "Bearer"
        assert "s3cret" not in r.text


def test_metrics_token_is_env_only(tmp_path: Path) -> None:
    from scruple.settings import SettingsError, load_settings

    f = tmp_path / "c.toml"
    f.write_text('metrics_token = "x"\n')
    with pytest.raises(SettingsError, match="environment"):
        load_settings(f, env={})


def test_latency_is_labelled_by_route_template() -> None:
    client = _client()
    labels = {"method": "GET", "route": "/rules/{rule_id}", "status": "404"}
    before = _sample(LATENCY, **labels)
    for rid in ("no-such-rule", "another-missing"):
        assert client.get(f"/rules/{rid}").status_code == 404
    assert _sample(LATENCY, **labels) == before + 2


def test_unrouted_paths_share_one_label() -> None:
    client = _client()
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = _sample(LATENCY, **labels)
    client.get("/nothing/here/1")
    client.get("/nothing/here/2")
    assert _sample(LATENCY, **labels) == before + 2


@settings(max_examples=25, deadline=None)
@given(st.from_regex(RULE_ID_PATTERN, fullmatch=True))
def test_route_label_never_carries_ids(rule_id: str) -> None:
    client = _shared_client()
    client.get(f"/rules/{rule_id}")
    routes = {
        s.labels["route"]
        for m in REGISTRY.collect()
        if m.name == "scruple_http_request_duration_seconds"
        for s in m.samples
    }
    assert rule_id not in routes


@functools.cache
def _shared_client() -> TestClient:
    return _client()


def test_in_flight_returns_to_zero() -> None:
    client = _client()
    client.get("/health")
    assert _sample("scruple_http_requests_in_flight", method="GET") == 0


def _metric(text: str, name: str, **labels: str) -> float:
    lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    pattern = rf"^{re.escape(name)}{re.escape('{' + lab + '}') if lab else ''} (\S+)$"
    m = re.search(pattern, text, re.M)
    return float(m.group(1)) if m else 0.0


def _register_and_evaluate(client: TestClient) -> None:
    body = {
        "rule_id": GOOD_RULE_ID,
        "spec": GOOD_RULE.model_dump(mode="json"),
        "owner": "payments-ops",
        "provenance": {"agent_id": "dispute-agent-01", "episode_ids": ["ep-1"]},
    }
    assert client.post("/rules", json=body, headers=AGENT).status_code == 201
    r = client.post(f"/rules/{GOOD_RULE_ID}/versions/1/evaluate", headers=AGENT)
    assert [d["outcome"] for d in r.json()] == ["promote", "canary_passed"]


def test_gate_decisions_are_counted_by_stage_and_outcome() -> None:
    client = _client(metrics_public=True)
    name = "scruple_gate_decisions_total"
    assert _metric(client.get("/metrics").text, name, stage="shadow", outcome="promote") == 0
    _register_and_evaluate(client)
    for _ in range(2):  # scraping again must not count twice
        text = client.get("/metrics").text
        assert _metric(text, name, stage="shadow", outcome="promote") == 1
        assert _metric(text, name, stage="canary", outcome="canary_passed") == 1


def test_decisions_from_other_replicas_are_counted(tmp_path: Path) -> None:
    db = f"sqlite:///{tmp_path / 'shared.db'}"
    writer = AgentGate.open(db, SteppingClock(), GateConfig(n_cases=1_500, bootstrap_resamples=300))
    writer.bootstrap_baseline()
    reader = AgentGate.open(db)
    scraper = TestClient(create_app(reader, settings=Settings(metrics_public=True)))
    before = scraper.get("/metrics").text
    _register_and_evaluate(
        TestClient(create_app(writer, settings=Settings.model_validate(DEV)), headers=READER)
    )
    after = scraper.get("/metrics").text
    name = "scruple_gate_decisions_total"
    assert _metric(before, name, stage="canary", outcome="canary_passed") == 0
    assert _metric(after, name, stage="canary", outcome="canary_passed") == 1
    assert _metric(after, "scruple_audit_chain_length") == len(writer.audit.entries())


def test_audit_chain_gauge_matches_the_chain() -> None:
    g = _gate()
    client = TestClient(create_app(g, settings=Settings(metrics_public=True)))
    n = _metric(client.get("/metrics").text, "scruple_audit_chain_length")
    assert n == len(g.audit.entries()) > 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _health_count(base: str) -> float:
    with urllib.request.urlopen(f"{base}/metrics", timeout=10) as r:
        text = r.read().decode()
    return _metric(
        text,
        "scruple_http_request_duration_seconds_count",
        method="GET",
        route="/health",
        status="200",
    )


def test_gunicorn_workers_share_metrics_through_the_multiproc_dir(tmp_path: Path) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRUPLE_")}
    env |= {
        "SCRUPLE_DATABASE_URL": f"sqlite:///{tmp_path / 'm.db'}",
        "SCRUPLE_API_PORT": str(port),
        "SCRUPLE_WEB_WORKERS": "3",
        "SCRUPLE_METRICS_PUBLIC": "true",
        "SCRUPLE_LOG_LEVEL": "WARNING",
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "metrics"),
        "TMPDIR": str(tmp_path),
    }
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "counter_stale.db").write_bytes(b"left over")
    proc = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "gunicorn"),
            "--config",
            str(ROOT / "deploy" / "docker" / "gunicorn.conf.py"),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"{base}/health", timeout=2).close()
                break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    out, _ = proc.communicate(timeout=10)
                    pytest.fail(out.decode(errors="replace"))
                time.sleep(0.2)
        assert not (tmp_path / "metrics" / "counter_stale.db").exists()
        before = _health_count(base)
        for _ in range(30):
            urllib.request.urlopen(f"{base}/health", timeout=10).close()
        # Every worker's requests, whichever worker served the scrape.
        assert _health_count(base) == before + 30
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
