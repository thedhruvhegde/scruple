"""Package metadata: what pip and PyPI see. Fast; no build."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _project() -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data


def _name(req: str) -> str:
    m = re.match(r"[A-Za-z0-9_.-]+", req)
    assert m, req
    return m.group(0).lower().replace("_", "-")


def test_distribution_name_and_import_name() -> None:
    p = _project()["project"]
    assert p["name"] == "scruple-engine"
    assert p["requires-python"] == ">=3.12,<3.13"
    assert (ROOT / "scruple" / "__init__.py").is_file()


def test_no_licence_is_claimed_until_the_founder_picks_one() -> None:
    p = _project()["project"]
    assert "license" not in p
    assert "license-files" not in p
    assert not [c for c in p["classifiers"] if c.startswith("License")]
    assert (ROOT / "LICENSE-PENDING.md").is_file()


def test_classifiers_are_well_formed() -> None:
    classifiers = _project()["project"]["classifiers"]
    assert len(classifiers) == len(set(classifiers))
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Typing :: Typed" in classifiers
    for c in classifiers:
        assert " :: " in c and c == c.strip()


def test_typed_marker_ships() -> None:
    assert (ROOT / "scruple" / "py.typed").is_file()


def test_entry_points() -> None:
    scripts = _project()["project"]["scripts"]
    assert scripts["scruple"] == "scruple.cli.main:main"
    assert scripts["agent-gate"] == "scruple.cli.main:main"


@pytest.mark.parametrize("extra", ["postgres", "agents", "claude", "langgraph", "pdf", "dev"])
def test_extra_exists(extra: str) -> None:
    extras = _project()["project"]["optional-dependencies"]
    assert extras[extra], extra


def test_extras_agree_with_core_pins() -> None:
    """An extra that repeats a core dependency must pin it the same way."""
    p = _project()["project"]
    core = {_name(r): r for r in p["dependencies"]}
    for extra, reqs in p["optional-dependencies"].items():
        for req in reqs:
            if _name(req) in core:
                assert req == core[_name(req)], (extra, req)


def test_dev_extra_mirrors_the_dev_group() -> None:
    p = _project()
    group = {_name(r): r for r in p["dependency-groups"]["dev"]}
    for req in p["project"]["optional-dependencies"]["dev"]:
        assert group.get(_name(req)) == req, req


def test_sdist_keeps_data_and_results_out() -> None:
    include = _project()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/scruple" in include and "/pyproject.toml" in include
    for path in include:
        assert (ROOT / path.lstrip("/")).exists(), path
    for private in ("/data", "/results", "/site", "/.scruple", "/tests"):
        assert private not in include
