"""Version and build metadata, shown by `scruple version` and `GET /version`.

The package version comes from the installed distribution metadata. The commit and
build date come from the environment (`SCRUPLE_BUILD_COMMIT`, `SCRUPLE_BUILD_DATE`),
which the Dockerfile sets from build args. Outside a built image the commit falls
back to `git rev-parse HEAD` when the source is a git checkout, else "unknown".
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from functools import cache
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ConfigDict

UNKNOWN = "unknown"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class BuildInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    commit: str
    build_date: str
    python: str

    def line(self) -> str:
        short = self.commit[:12]
        return (
            f"{self.name} {self.version} (commit {short}, built {self.build_date}, "
            f"python {self.python})"
        )


# Published as `scruple-engine`; `scruple` is the distribution name before the rename,
# still found in environments synced before it.
DISTRIBUTIONS = ("scruple-engine", "scruple")


def package_version() -> str:
    for dist in DISTRIBUTIONS:
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return "0.0.0+unknown"  # pragma: no cover - only when run from a raw tree


@cache
def _git_commit() -> str:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return UNKNOWN
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH is intended
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return out if _SHA.match(out) else UNKNOWN


def build_info(env: dict[str, str] | None = None) -> BuildInfo:
    e = os.environ if env is None else env
    commit = e.get("SCRUPLE_BUILD_COMMIT", "").strip().lower()
    if not _SHA.match(commit):
        commit = _git_commit()
    return BuildInfo(
        name="scruple",
        version=package_version(),
        commit=commit,
        build_date=e.get("SCRUPLE_BUILD_DATE", "").strip() or UNKNOWN,
        python=platform.python_version(),
    )
