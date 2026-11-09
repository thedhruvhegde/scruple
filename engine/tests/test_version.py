from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.cli.main import main
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage
from scruple.version import UNKNOWN, BuildInfo, build_info, package_version

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert package_version() == pyproject["project"]["version"]


def test_build_metadata_comes_from_env() -> None:
    info = build_info({"SCRUPLE_BUILD_COMMIT": SHA.upper(), "SCRUPLE_BUILD_DATE": "2026-01-02"})
    assert info.commit == SHA
    assert info.build_date == "2026-01-02"
    assert SHA[:12] in info.line() and info.version in info.line()


@pytest.mark.parametrize("bad", ["", "not-a-sha", "zz" * 20, "; rm -rf /"])
def test_invalid_commit_falls_back(bad: str) -> None:
    info = build_info({"SCRUPLE_BUILD_COMMIT": bad})
    assert info.commit != bad
    assert info.commit == UNKNOWN or len(info.commit) == 40
    assert info.build_date == UNKNOWN


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith(f"scruple {package_version()} (commit ")
    assert main(["version", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert BuildInfo.model_validate(data).version == package_version()


def test_api_version() -> None:
    client = TestClient(create_app(AgentGate(SQLiteStorage(":memory:"))))
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["version"] == package_version()
    assert client.get("/openapi.json").json()["info"]["version"] == package_version()
