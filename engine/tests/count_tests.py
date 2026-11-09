"""Count the test suite by category: `uv run python -m tests.count_tests [--markdown]`.

Collects every test (default and slow) with pytest's own collector and groups the
test ids by the file they live in (see CATEGORIES), and separately by kind: hypothesis
property tests, stateful machines, and slow/postgres markers. docs/TESTING.md pastes
its output.
"""

from __future__ import annotations

import io
import sys
from collections import Counter
from contextlib import redirect_stdout
from typing import Any

import pytest

# (category, file name prefixes or exact names), first match wins
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stateful properties (161, 162)", ("test_props_",)),
    ("metamorphic (163)", ("test_metamorphic",)),
    ("golden episodes (164)", ("test_golden_episodes",)),
    ("api fuzzing (166)", ("test_api_fuzz",)),
    ("determinism (168)", ("test_determinism",)),
    ("load (169)", ("test_load",)),
    ("chaos / fault injection (170)", ("test_chaos",)),
    ("audit tamper (171)", ("test_audit_tamper",)),
    ("adapter contracts (172)", ("test_adapter_contracts",)),
    ("end to end in processes (173)", ("test_e2e_processes",)),
    ("storage backends", ("storage/",)),
    ("simulator", ("test_sim",)),
    ("gate and statistics", ("test_gate", "test_stats", "test_properties")),
    ("verifiers and attacks", ("test_adversarial", "test_known_gaps", "test_failure_modes")),
    ("registry, audit, service", ("test_registry", "test_audit", "test_service")),
    ("ingest adapters", ("test_ingest",)),
    ("agents and episodes", ("test_agents", "test_mcp")),
    ("real data (CFPB)", ("test_realdata",)),
    ("ci, evidence packs", ("test_ci",)),
    ("api, cli, settings, ops", ("test_api", "test_cli", "test_strict", "test_settings")),
)


def category(nodeid: str) -> str:
    path = nodeid.split("::", 1)[0].removeprefix("tests/")
    for name, prefixes in CATEGORIES:
        if any(path.startswith(p) for p in prefixes):
            return name
    return "other (demo, logging, version, deploy, alias)"


class _Collector:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        self.items = list(items)


def collect() -> list[dict[str, Any]]:
    c = _Collector()
    with redirect_stdout(io.StringIO()):
        code = pytest.main(
            ["--collect-only", "-q", "-p", "no:cacheprovider", "-m", "slow or not slow"],
            plugins=[c],
        )
    if code not in (0, 5):
        raise SystemExit(f"collection failed ({code})")
    rows = []
    for it in c.items:
        fn = getattr(it, "obj", None)
        marks = {m.name for m in it.iter_markers()}
        rows.append(
            {
                "id": it.nodeid,
                "category": category(it.nodeid),
                "hypothesis": bool(getattr(fn, "is_hypothesis_test", False)),
                "stateful": it.nodeid.endswith("::runTest"),
                "slow": "slow" in marks,
                "postgres": "postgres" in marks,
            }
        )
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    by = Counter(r["category"] for r in rows)
    prop = Counter(r["category"] for r in rows if r["hypothesis"] or r["stateful"])
    slow = Counter(r["category"] for r in rows if r["slow"])
    pg = Counter(r["category"] for r in rows if r["postgres"])
    order = [n for n, _ in CATEGORIES] + ["other (demo, logging, version, deploy, alias)"]
    lines = [
        "| category | tests | hypothesis or stateful | slow | postgres |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in order:
        if by[name]:
            lines.append(f"| {name} | {by[name]} | {prop[name]} | {slow[name]} | {pg[name]} |")
    total = len(rows)
    lines.append(
        f"| **total** | **{total}** | **{sum(prop.values())}** | **{sum(slow.values())}** "
        f"| **{sum(pg.values())}** |"
    )
    machines = sum(r["stateful"] for r in rows)
    default = sum(not r["slow"] for r in rows)
    lines.append("")
    lines.append(
        f"{default} run by default (`make test`), {total - default} only in `make test-slow`; "
        f"{machines} of the property tests are stateful machines."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(table(collect()))
    sys.exit(0)
