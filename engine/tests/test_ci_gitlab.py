"""Sanity checks for the GitLab CI template (structure only; no runner)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parent.parent / ".gitlab" / "scruple-gate.yml"


@pytest.fixture(scope="module")
def tpl() -> dict[str, Any]:
    data = yaml.safe_load(TEMPLATE.read_text())
    assert isinstance(data, dict)
    return data


def test_hidden_base_job_and_concrete_job(tpl: dict[str, Any]) -> None:
    assert tpl["scruple-gate"] == {"extends": ".scruple-gate"}
    base = tpl[".scruple-gate"]
    assert base["image"].startswith("python:3.12")
    assert {"script", "before_script", "artifacts", "rules"} <= set(base)


def test_variables_have_defaults(tpl: dict[str, Any]) -> None:
    v = tpl["variables"]
    assert v["SCRUPLE_CONFIG"] == "scruple.toml"
    assert v["SCRUPLE_SCORECARD"].endswith(".json")
    assert all(isinstance(x, str) for x in v.values())


def test_uses_the_same_cli_and_propagates_exit_code(tpl: dict[str, Any]) -> None:
    script = tpl[".scruple-gate"]["script"]
    joined = "\n".join(script)
    assert 'scruple ci run --config "$SCRUPLE_CONFIG"' in joined
    assert "scruple ci render" in joined
    assert script[-1] == "exit $code"


def test_artifacts_kept_even_when_the_gate_fails(tpl: dict[str, Any]) -> None:
    art = tpl[".scruple-gate"]["artifacts"]
    assert art["when"] == "always"
    assert set(art["paths"]) == {"$SCRUPLE_SCORECARD", "$SCRUPLE_MARKDOWN"}


def test_runs_on_merge_requests(tpl: dict[str, Any]) -> None:
    rules = [r["if"] for r in tpl[".scruple-gate"]["rules"]]
    assert any("merge_request_event" in r for r in rules)


def test_install_requires_explicit_requirement(tpl: dict[str, Any]) -> None:
    before = "\n".join(tpl[".scruple-gate"]["before_script"])
    assert 'pip install "$SCRUPLE_INSTALL"' in before
    assert "exit 2" in before


@pytest.mark.parametrize(("which", "code"), [("good", 0), ("bad", 1)])
def test_script_runs_locally_with_real_cli(
    tpl: dict[str, Any], tmp_path: Path, which: str, code: int
) -> None:
    """Run the template's script lines in bash, as a runner would, against the CLI."""
    import os
    import subprocess
    import sys

    from tests.ci_support import BAD, GOOD, write_repo

    write_repo(tmp_path, GOOD if which == "good" else BAD)
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        **{k: str(v) for k, v in tpl["variables"].items()},
    }
    script = "set -eo pipefail\n" + "\n".join(tpl[".scruple-gate"]["script"])
    r = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert r.returncode == code, r.stderr
    assert (tmp_path / "scruple-scorecard.json").exists()
    assert (tmp_path / "scruple-scorecard.md").read_text().startswith("<!-- scruple-gate -->")
