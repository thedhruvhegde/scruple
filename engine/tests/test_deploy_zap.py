"""Shape checks for the ZAP baseline workflow (deploy/github-workflows/zap.yml).

It only runs once copied to the monorepo root, so the parts that keep it safe are
pinned here: read-only permissions, SHA-pinned actions, no issue writing, a
throwaway database, teardown always. actionlint runs too when it is on PATH.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "deploy" / "github-workflows" / "zap.yml"
RULES = ROOT / "deploy" / "zap" / "rules.tsv"


def _load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def _steps() -> list[dict[str, Any]]:
    steps = _load()["jobs"]["baseline"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_triggers_and_engine_paths() -> None:
    on = _load()[True]  # PyYAML reads the bare key `on` as True
    assert "engine/**" in on["pull_request"]["paths"]
    assert "engine/**" in on["push"]["paths"]
    assert on["schedule"] and "workflow_dispatch" in on
    assert _load()["defaults"]["run"]["working-directory"] == "engine"


def test_permissions_are_read_only() -> None:
    wf = _load()
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["baseline"]["permissions"] == {"contents": "read"}


def test_actions_are_pinned_to_commit_shas() -> None:
    uses = [s["uses"] for s in _steps() if "uses" in s]
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u), u
    text = WORKFLOW.read_text()
    for u in uses:
        line = next(line for line in text.splitlines() if u in line)
        assert re.search(r"# v\d+\.\d+\.\d+$", line), line


def test_zap_step_targets_local_api_without_issue_writing() -> None:
    (zap,) = [s for s in _steps() if str(s.get("uses", "")).startswith("zaproxy/action-baseline")]
    w = zap["with"]
    assert w["target"] == "http://127.0.0.1:8000/console/"
    assert w["allow_issue_writing"] is False
    assert w["fail_action"] is True
    assert "-I" in w["cmd_options"].split()
    assert w["rules_file_name"] == "engine/deploy/zap/rules.tsv"
    assert zap["env"]["ZAP_AUTH_HEADER_SITE"] == "127.0.0.1"


def test_database_password_is_random_and_masked() -> None:
    start = next(s for s in _steps() if "docker compose up" in s.get("run", ""))
    assert "openssl rand" in start["run"] and "::add-mask::" in start["run"]
    seed = next(s for s in _steps() if "keys create" in s.get("run", ""))
    assert "--role auditor" in seed["run"] and "::add-mask::" in seed["run"]


def test_teardown_always_runs() -> None:
    last = _steps()[-1]
    assert last["if"] == "always()" and "docker compose down --volumes" in last["run"]


def test_rules_file_is_tab_separated_with_known_actions() -> None:
    lines = [ln for ln in RULES.read_text().splitlines() if ln.strip()]
    assert lines
    for ln in lines:
        rule_id, action, comment = ln.split("\t")
        assert rule_id.isdigit() and action in {"IGNORE", "INFO", "WARN", "FAIL"}
        assert comment.startswith("(") and comment.endswith(")")
    # nothing is silenced without a written reason in docs/trust/PENTEST_SCOPE.md
    assert all("\tIGNORE\t" not in ln for ln in lines)


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_actionlint_is_clean() -> None:
    out = subprocess.run(["actionlint", str(WORKFLOW)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
