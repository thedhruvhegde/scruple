"""Production server: the hardened Dockerfile and gunicorn configured from settings.

The gunicorn boot test runs the real server (two uvicorn workers on a SQLite file),
checks it answers, then sends SIGTERM and expects a clean, graceful exit.
"""

from __future__ import annotations

import os
import re
import runpy
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from scruple.settings import SettingsError, load_settings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
GUNICORN_CONF = ROOT / "deploy" / "docker" / "gunicorn.conf.py"
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text()


def _final_stage() -> str:
    return DOCKERFILE[DOCKERFILE.rindex("\nFROM ") :]


@pytest.mark.parametrize(
    ("arg", "image"),
    [("PYTHON_IMAGE", r"python:3\.12-slim"), ("UV_IMAGE", r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+")],
)
def test_base_images_are_pinned_by_digest(arg: str, image: str) -> None:
    assert re.search(rf"^ARG {arg}={image}@sha256:[0-9a-f]{{64}}$", DOCKERFILE, re.M)


def test_final_stage_uses_tini_numeric_user_and_gunicorn() -> None:
    final = _final_stage()
    assert re.search(r"^USER 10001:10001$", final, re.M)
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "scruple-entrypoint"]' in final
    assert 'CMD ["gunicorn", "--config", "/app/gunicorn.conf.py"]' in final
    assert "STOPSIGNAL SIGTERM" in final and "HEALTHCHECK" in final
    assert "PYTHONDONTWRITEBYTECODE=1" in final and "TMPDIR=/tmp" in final


def test_entrypoint_can_skip_migrations_and_runs_gunicorn_directly() -> None:
    assert 'SCRUPLE_MIGRATE_ON_START:-true}" = "true"' in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT and ENTRYPOINT.rstrip().endswith('exec scruple "$@"')


def _conf(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, object]:
    for k in list(os.environ):
        if k.startswith("SCRUPLE_"):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return runpy.run_path(str(GUNICORN_CONF))


def test_gunicorn_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _conf(monkeypatch)
    assert c["bind"] == ["127.0.0.1:8000"]
    assert c["workers"] == 1 and c["worker_class"] == "uvicorn_worker.UvicornWorker"
    assert c["wsgi_app"] == "scruple.api.app:create_app()"
    assert c["preload_app"] is False and c["accesslog"] is None
    assert c["max_requests"] == 0
    assert c["control_socket_disable"] is True  # $HOME is read-only in the image


def test_gunicorn_reads_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _conf(
        monkeypatch,
        SCRUPLE_API_HOST="0.0.0.0",
        SCRUPLE_API_PORT="9001",
        SCRUPLE_WEB_WORKERS="4",
        SCRUPLE_WEB_TIMEOUT="90",
        SCRUPLE_WEB_GRACEFUL_TIMEOUT="20",
        SCRUPLE_WEB_KEEPALIVE="75",
        SCRUPLE_WEB_MAX_REQUESTS="1000",
        SCRUPLE_LOG_LEVEL="WARNING",
    )
    assert c["bind"] == ["0.0.0.0:9001"]
    assert (c["workers"], c["timeout"], c["graceful_timeout"], c["keepalive"]) == (4, 90, 20, 75)
    assert (c["max_requests"], c["max_requests_jitter"], c["loglevel"]) == (1000, 100, "warning")


def test_gunicorn_brackets_ipv6_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _conf(monkeypatch, SCRUPLE_API_HOST="::")["bind"] == ["[::]:8000"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SCRUPLE_WEB_WORKERS", "0"),
        ("SCRUPLE_WEB_WORKERS", "65"),
        ("SCRUPLE_WEB_TIMEOUT", "0"),
        ("SCRUPLE_WEB_GRACEFUL_TIMEOUT", "-1"),
        ("SCRUPLE_WEB_MAX_REQUESTS", "-5"),
    ],
)
def test_web_settings_are_bounded(name: str, value: str) -> None:
    with pytest.raises(SettingsError):
        load_settings(env={name: value})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=2) as r:
        return int(r.status), r.read()


def test_gunicorn_serves_with_two_workers_and_stops_gracefully(tmp_path: Path) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRUPLE_")}
    env |= {
        "SCRUPLE_DATABASE_URL": f"sqlite:///{tmp_path / 'g.db'}",
        "SCRUPLE_API_PORT": str(port),
        "SCRUPLE_WEB_WORKERS": "2",
        "SCRUPLE_WEB_GRACEFUL_TIMEOUT": "5",
        "SCRUPLE_LOG_LEVEL": "WARNING",
        "TMPDIR": str(tmp_path),
    }
    gunicorn = Path(sys.executable).parent / "gunicorn"
    proc = subprocess.Popen(
        [str(gunicorn), "--config", str(GUNICORN_CONF)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        status, body = 0, b""
        while time.monotonic() < deadline:
            try:
                status, body = _get(f"http://127.0.0.1:{port}/health")
                break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
        if status != 200:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
            pytest.fail(f"gunicorn did not serve /health:\n{out.decode(errors='replace')}")
        assert b'"ok"' in body
        # Baseline imported once by the master; both workers see the same rules.
        for _ in range(6):
            assert _get(f"http://127.0.0.1:{port}/rules")[0] == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
