"""Schema sanity for the GitHub composite action (no network, no runner)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "scruple-gate" / "action.yml"


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    data = yaml.safe_load(ACTION.read_text())
    assert isinstance(data, dict)
    return data


def _steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    steps = action["runs"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_top_level_keys(action: dict[str, Any]) -> None:
    assert {"name", "description", "inputs", "outputs", "runs"} <= set(action)
    assert action["runs"]["using"] == "composite"


def test_inputs_are_described_with_string_defaults(action: dict[str, Any]) -> None:
    for name, spec in action["inputs"].items():
        assert re.fullmatch(r"[a-z][a-z0-9-]*", name), name
        assert spec.get("description"), name
        assert "default" in spec and isinstance(spec["default"], str), name
    assert action["inputs"]["config"]["default"] == "scruple.toml"


def test_every_run_step_declares_a_shell(action: dict[str, Any]) -> None:
    for step in _steps(action):
        assert ("run" in step) != ("uses" in step), step
        if "run" in step:
            assert step["shell"] == "bash"


def test_run_scripts_never_interpolate_expressions(action: dict[str, Any]) -> None:
    """Inputs reach scripts through env only (avoids script injection from PR data)."""
    for step in _steps(action):
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")


def test_outputs_point_at_existing_step_ids(action: dict[str, Any]) -> None:
    ids = {s["id"] for s in _steps(action) if "id" in s}
    for name, spec in action["outputs"].items():
        assert spec.get("description"), name
        for ref in re.findall(r"steps\.([a-z0-9_-]+)\.outputs", spec["value"]):
            assert ref in ids, (name, ref)


def test_runs_ci_run_and_fails_on_block(action: dict[str, Any]) -> None:
    scripts = "\n".join(s.get("run", "") for s in _steps(action))
    assert "ci run --config" in scripts
    assert _steps(action)[-1]["run"].strip() == 'exit "$SCRUPLE_EXIT"'
    uses = [s["uses"] for s in _steps(action) if "uses" in s]
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", u) for u in uses)
