"""Build the wheel and sdist, install the wheel into a fresh venv, run the CLI from it.

Slow (a build plus a dependency install, tens of seconds with a warm uv cache) and
needs `uv` plus access to a package index, so it runs with `make test-slow` and in
the release workflow, not in the default suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UV = shutil.which("uv")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(UV is None, reason="uv is not on PATH"),
]


def _run(*cmd: str | Path, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    out = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )
    assert out.returncode == 0, f"{cmd}\n{out.stdout}\n{out.stderr}"
    return out.stdout


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert UV is not None
    out = tmp_path_factory.mktemp("dist")
    _run(UV, "build", "--out-dir", out, ROOT, cwd=ROOT)
    return out


def _version() -> str:
    v: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return v


def test_build_produces_wheel_and_sdist(dist: Path) -> None:
    v = _version()
    wheel = dist / f"scruple_engine-{v}-py3-none-any.whl"
    sdist = dist / f"scruple_engine-{v}.tar.gz"
    assert wheel.is_file() and sdist.is_file()

    names = zipfile.ZipFile(wheel).namelist()
    assert "scruple/py.typed" in names
    assert "scruple/cli/main.py" in names
    assert any(n.startswith("scruple/sim/scenarios/") and n.endswith(".yaml") for n in names)
    assert not [n for n in names if n.startswith(("tests/", "data/", "results/"))]
    meta = zipfile.ZipFile(wheel).read(f"scruple_engine-{v}.dist-info/METADATA").decode()
    assert "Name: scruple-engine" in meta
    assert "Provides-Extra: postgres" in meta
    assert "License" not in "".join(ln for ln in meta.splitlines() if ln.startswith("Class"))

    with tarfile.open(sdist) as tar:
        members = tar.getnames()
    assert f"scruple_engine-{v}/pyproject.toml" in members
    top = {m.split("/")[1] for m in members if m.count("/") >= 1}
    assert not top & {"data", "results", "site", "tests", ".scruple", ".env"}


def test_wheel_installs_into_a_fresh_venv(dist: Path, tmp_path: Path) -> None:
    assert UV is not None
    v = _version()
    venv = tmp_path / "venv"
    _run(UV, "venv", "--python", "3.12", venv, cwd=tmp_path)
    py = venv / "bin" / "python"
    wheel = dist / f"scruple_engine-{v}-py3-none-any.whl"
    _run(UV, "pip", "install", "--python", py, wheel, cwd=tmp_path)

    scruple = venv / "bin" / "scruple"
    assert _run(scruple, "version", cwd=tmp_path).startswith(f"scruple {v} (commit ")
    assert "usage: scruple demo" in _run(scruple, "demo", "--help", cwd=tmp_path)
    # The import comes from the venv, not this checkout.
    where = _run(py, "-c", "import scruple; print(scruple.__file__)", cwd=tmp_path)
    assert str(venv) in where
