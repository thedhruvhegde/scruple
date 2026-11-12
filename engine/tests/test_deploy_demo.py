"""The hosted demo recipe (deploy/demo/): compose shape, edge rules, and a real seed run.

The seed test runs deploy/demo/seed.sh against this checkout's CLI (no container),
then serves the result in-process and checks the visitor key can read and not write.
Docker and Caddy are optional: their checks skip when the binaries are missing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.service import AgentGate
from scruple.settings import load_settings

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "deploy" / "demo"
ENV = {"DEMO_DOMAIN": "demo.example.com", "ACME_EMAIL": "ops@example.com"}


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_config_is_valid_and_hardened() -> None:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "--profile",
            "tools",
            "config",
            "--format",
            "json",
        ],
        cwd=DEMO,
        env={**os.environ, **ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 and "is not a docker command" in out.stderr:
        pytest.skip("docker compose plugin not installed")
    assert out.returncode == 0, out.stderr
    svc = json.loads(out.stdout)["services"]
    api, caddy, seed = svc["api"], svc["caddy"], svc["seed"]
    assert "ports" not in api  # only Caddy is reachable from outside
    assert {p["target"] for p in caddy["ports"]} == {80, 443}
    for s in (api, seed):
        assert s["read_only"] and s["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in s["security_opt"]
    assert api["environment"]["SCRUPLE_DATABASE_URL"] == "/data/demo.db"
    assert "SCRUPLE_METRICS_PUBLIC" not in api["environment"]
    assert "SCRUPLE_AUTH_DEV_ACTOR_HEADER" not in api["environment"]
    assert seed["profiles"] == ["tools"]
    assert not any("PASSWORD" in k or "SALT" in k for k in api["environment"])


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_refuses_to_start_without_a_domain() -> None:
    # Only the domain is missing: compose versions differ in which unset required
    # variable they report first, so every other one is provided.
    env = {k: v for k, v in os.environ.items() if k not in ENV}
    env["ACME_EMAIL"] = ENV["ACME_EMAIL"]
    out = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--quiet"],
        cwd=DEMO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0 and "DEMO_DOMAIN" in out.stderr


def test_caddyfile_is_read_only_and_hides_metrics() -> None:
    text = (DEMO / "Caddyfile").read_text()
    assert "@write not method GET HEAD" in text and "respond @write" in text
    assert "/metrics" in text and "/auth/keys" in text
    assert "admin off" in text
    assert "log_credentials" not in text  # Caddy redacts Authorization unless this is set


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy not installed")
def test_caddyfile_validates() -> None:
    out = subprocess.run(
        ["caddy", "validate", "--config", "Caddyfile", "--adapter", "caddyfile"],
        cwd=DEMO,
        env={**os.environ, **ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr


def test_scripts_are_executable_and_strict() -> None:
    for name in ("seed.sh", "reset.sh"):
        p = DEMO / name
        assert p.stat().st_mode & stat.S_IXUSR
        assert "set -eu" in p.read_text()
    assert "demo.env" in (DEMO / ".gitignore").read_text()
    assert "out/" in (DEMO / ".gitignore").read_text()


def test_seed_builds_a_synthetic_demo_and_a_read_only_key(tmp_path: Path) -> None:
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    env = {
        **os.environ,
        "SCRUPLE_BIN": f"{sys.executable} -m scruple.cli.main",
        "DEMO_DATA": str(data),
        "DEMO_RULES": str(ROOT / "docs" / "pilot" / "demo"),
        "DEMO_OUT": str(out),
    }
    for step in ("build", "swap"):
        done = subprocess.run(
            ["sh", str(DEMO / "seed.sh"), step],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    assert "INTEGRITY OK" in done.stdout or (data / "demo.db").exists()
    key_file = out / "visitor-key.json"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    key = json.loads(key_file.read_text())
    assert key["roles"] == ["auditor"] and key["tenant"] == "demo" and key["expires_at"]

    g = AgentGate.open(str(data / "demo.db"))
    client = TestClient(create_app(g, load_settings(env={})))
    h = {"Authorization": f"Bearer {key['api_key']}"}
    assert client.get("/auth/whoami", headers=h).json()["permissions"] == ["read"]
    rules = {r["rule_id"]: r["status"] for r in client.get("/rules", headers=h).json()}
    assert rules["inr-delivered-contest"] == "active"
    assert rules["inr-delivered-contest-fast"] == "rejected"
    assert client.get("/audit/verify", headers=h).json()["ok"] is True
    body = json.loads((ROOT / "docs" / "pilot" / "demo" / "good.json").read_text())
    assert client.post("/rules", json=body, headers=h).status_code == 403
    assert client.get("/rules").status_code == 401
    # a second build leaves the served database alone until swap
    done = subprocess.run(
        ["sh", str(DEMO / "seed.sh"), "build"], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0 and (data / "demo.db.new").exists()


def test_swap_without_a_build_fails(tmp_path: Path) -> None:
    done = subprocess.run(
        ["sh", str(DEMO / "seed.sh"), "swap"],
        env={**os.environ, "DEMO_DATA": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1 and "nothing to swap" in done.stderr
