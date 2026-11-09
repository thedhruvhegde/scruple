"""Container and compose files: shape checks that run without a Docker daemon."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scruple.cli.main import main

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _final_stage(dockerfile: str) -> str:
    return dockerfile[dockerfile.rindex("\nFROM ") :]


def test_final_image_is_slim_and_runs_as_non_root() -> None:
    final = _final_stage(DOCKERFILE)
    assert re.search(r"^FROM python:\$\{PYTHON_VERSION\}-slim$", final, re.M)
    users = re.findall(r"^USER (\S+)", final, re.M)
    assert users and users[-1].split(":")[0] not in {"root", "0"}


def test_uv_image_is_pinned_and_lockfile_is_frozen() -> None:
    assert re.search(r"^FROM ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ AS uv$", DOCKERFILE, re.M)
    assert DOCKERFILE.count("--frozen") == 2 and "--no-dev" in DOCKERFILE


def test_no_secrets_baked_into_the_image() -> None:
    assert not re.search(r"(PASSWORD|SECRET|TOKEN)\s*=", DOCKERFILE)
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".git", "*.db"} <= set(ignored)


def test_entrypoint_is_executable_and_migrates_postgres_only() -> None:
    ep = ROOT / "docker" / "entrypoint.sh"
    assert os.access(ep, os.X_OK)
    text = ep.read_text()
    assert "python -m scruple.storage.migrate" in text and 'exec scruple "$@"' in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_config_is_valid_and_keeps_the_password_out() -> None:
    env = {**os.environ, "POSTGRES_PASSWORD": "from-the-environment"}
    out = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 and "compose" in out.stderr and "is not a docker command" in out.stderr:
        pytest.skip("docker compose plugin not installed")
    assert out.returncode == 0, out.stderr
    cfg = json.loads(out.stdout)
    api, pg = cfg["services"]["api"], cfg["services"]["postgres"]
    assert pg["image"].startswith("postgres:16")
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["environment"]["SCRUPLE_DATABASE_URL"].startswith("postgresql://")
    assert (
        ":" not in api["environment"]["SCRUPLE_DATABASE_URL"].split("@")[0][len("postgresql://") :]
    )
    assert api["environment"]["SCRUPLE_DATABASE_PASSWORD"] == "from-the-environment"
    raw = (ROOT / "docker-compose.yml").read_text()
    assert "from-the-environment" not in raw and "${POSTGRES_PASSWORD:?" in raw


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_refuses_to_start_without_a_password() -> None:
    env = {k: v for k, v in os.environ.items() if k != "POSTGRES_PASSWORD"}
    out = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0 and "POSTGRES_PASSWORD" in out.stderr


def test_cli_uses_the_settings_database_when_no_db_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "from-env.db"
    monkeypatch.setenv("SCRUPLE_DATABASE_URL", f"sqlite:///{db}")
    assert main(["rules"]) == 0
    assert db.exists() and "base-fx-contest@v1" in capsys.readouterr().out
