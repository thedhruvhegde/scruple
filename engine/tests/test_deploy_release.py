"""Shape checks for the container release workflow (deploy/github-workflows/release.yml).

The workflow only runs once copied to the monorepo root; these tests pin the parts
that matter for supply-chain safety so a careless edit fails here first. actionlint
runs too when it is on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "deploy" / "github-workflows" / "release.yml"


def _load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def _steps() -> list[dict[str, Any]]:
    steps = _load()["jobs"]["image"]["steps"]
    assert isinstance(steps, list)
    return steps


def _uses(prefix: str) -> list[dict[str, Any]]:
    return [s for s in _steps() if str(s.get("uses", "")).startswith(prefix)]


def test_triggers_are_engine_tags_and_engine_paths() -> None:
    # PyYAML reads the bare key `on` as True.
    on = _load()[True]
    assert on["push"]["tags"] == ["engine-v*.*.*"]
    assert on["push"]["branches"] == ["main"]
    assert "engine/**" in on["push"]["paths"]
    assert "pull_request" not in on  # never push images from untrusted refs


def test_runs_inside_engine_directory() -> None:
    assert _load()["defaults"]["run"]["working-directory"] == "engine"
    for s in _uses("docker/build-push-action"):
        assert s["with"]["context"] == "engine"
        assert s["with"]["file"] == "engine/Dockerfile"


def test_permissions_are_least_privilege() -> None:
    wf = _load()
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["image"]["permissions"]
    assert job["packages"] == "write"
    assert job["id-token"] == "write"
    assert "write" not in {wf["permissions"]["contents"], job["contents"]}


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_push_builds_both_architectures_with_sbom_and_provenance(platform: str) -> None:
    pushed = [s for s in _uses("docker/build-push-action") if s["with"].get("push") is True]
    assert len(pushed) == 1
    w = pushed[0]["with"]
    assert platform in w["platforms"].split(",")
    assert w["sbom"] is True and w["provenance"] == "mode=max"


def test_image_goes_to_ghcr_with_github_token() -> None:
    assert _load()["env"]["IMAGE"] == "ghcr.io/thedhruvhegde/scruple-engine"
    (login,) = _uses("docker/login-action")
    assert login["with"]["registry"] == "${{ env.REGISTRY }}"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_tags_by_semver_and_sha() -> None:
    (meta,) = _uses("docker/metadata-action")
    tags = meta["with"]["tags"]
    assert "type=sha,format=long" in tags
    assert "pattern=engine-v(\\d+\\.\\d+\\.\\d+.*),group=1" in tags


def test_scan_gates_the_push_and_signing_follows_it() -> None:
    names = [str(s.get("name", s.get("uses", ""))) for s in _steps()]
    scan = next(i for i, s in enumerate(_steps()) if "trivy" in str(s.get("uses", "")))
    push = names.index("build and push (amd64 + arm64, SBOM, provenance)")
    sign = names.index("sign the digest (keyless)")
    assert scan < push < sign
    gate = _steps()[scan]["with"]
    assert gate["exit-code"] == "1" and "CRITICAL" in gate["severity"]


def test_cosign_signs_by_digest_keyless() -> None:
    assert _uses("sigstore/cosign-installer")
    sign = next(s for s in _steps() if s.get("name") == "sign the digest (keyless)")
    assert sign["run"] == 'cosign sign --yes "${IMAGE}@${DIGEST}"'
    assert "--key" not in sign["run"]
    verify = next(s for s in _steps() if s.get("name") == "verify the signature")
    assert "token.actions.githubusercontent.com" in verify["run"]


def test_every_action_is_version_pinned() -> None:
    for s in _steps():
        if "uses" in s:
            assert "@" in s["uses"], s["uses"]
            assert not s["uses"].endswith(("@main", "@master")), s["uses"]


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_actionlint_is_clean() -> None:
    out = subprocess.run(["actionlint", str(WORKFLOW)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
