"""One version: `project.version` in pyproject.toml. Everything else reads it back."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import scruple
from scruple.version import package_version

ROOT = Path(__file__).resolve().parent.parent
VERSION: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_version_is_pep440_release() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?", VERSION)


def test_dunder_version_is_the_distribution_version() -> None:
    assert scruple.__version__ == package_version() == VERSION
    with pytest.raises(AttributeError):
        _ = scruple.no_such_attribute


def test_mcp_servers_report_the_package_version() -> None:
    from scruple.mcp.gate_server import build_gate_server
    from scruple.mcp.sim_server import build_sim_server
    from scruple.service import AgentGate
    from scruple.storage import SQLiteStorage

    sim = build_sim_server()
    gate = build_gate_server(AgentGate(SQLiteStorage(":memory:")))
    for server in (sim, gate):
        assert server.version == VERSION


def test_no_hardcoded_version_in_the_package() -> None:
    """A literal copy of the version in the source goes stale on the next bump."""
    pattern = re.compile(r"""version\s*=\s*["']\d+\.\d+\.\d+["']""")
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "scruple").rglob("*.py")
        if pattern.search(p.read_text())
    ]
    assert offenders == []
