"""Every module-level third-party import in `scruple/` is installed by a plain
`pip install scruple-engine`: a declared dependency or something one of them pulls
in. CI regression: numpy came only from the dev group (via scikit-learn), so the
wheel and the container image crashed on `scruple version`."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Modules that only run from a dev checkout (`make formal`), documented as such.
DEV_ONLY = {"scruple/formal/bmc.py": {"z3"}}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _runtime_closure() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    todo = [_norm(re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0]) for d in deps]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = requires(name) or []
        except PackageNotFoundError:
            continue
        for r in reqs:
            if "extra ==" in r:
                continue
            todo.append(_norm(re.split(r"[<>=!~\[ ;(]", r, maxsplit=1)[0]))
    return seen


def _top_level_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out - set(sys.stdlib_module_names) - {"scruple", "agent_gate", "__future__"}


def test_module_level_imports_are_runtime_dependencies() -> None:
    closure = _runtime_closure()
    dists = packages_distributions()
    missing: dict[str, set[str]] = {}
    for path in sorted((ROOT / "scruple").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for mod in _top_level_imports(path) - DEV_ONLY.get(rel, set()):
            if not {_norm(d) for d in dists.get(mod, [mod])} & closure:
                missing.setdefault(mod, set()).add(rel)
    assert not missing, missing


def test_numpy_is_declared() -> None:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(_norm(d).startswith("numpy") for d in deps)
