"""Helm chart (deploy/helm/scruple): secrets only by reference, hardened pods, the
migration hook, and render-time guard rails. Rendering tests need `helm` on PATH;
kubeconform, when present, validates the output against Kubernetes schemas."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy" / "helm" / "scruple"
KUBE = "1.30.0"
REQUIRED = ["--set", "database.passwordSecret.name=scruple-db"]

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "rel", str(CHART), "--kube-version", KUBE, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*args: str) -> list[dict[str, Any]]:
    out = _helm(*REQUIRED, *args)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, f"{kind}: {len(found)}"
    return found[0]


def _walk(node: object) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.append((str(k), v))
            out.extend(_walk(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v))
    return out


def test_values_hold_secret_references_never_values() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for ref in (
        values["database"]["passwordSecret"],
        values["metrics"]["tokenSecret"],
        values["heldoutSalt"]["secret"],
    ):
        assert set(ref) == {"name", "key"}
    assert ":" not in values["database"]["url"].split("@")[0].removeprefix("postgresql://")
    for key, _ in _walk(values):
        assert key.lower() not in {"password", "token", "secret_value", "salt"}


@needs_helm
def test_lint() -> None:
    out = subprocess.run(
        ["helm", "lint", str(CHART), "--kube-version", KUBE, *REQUIRED],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr


@needs_helm
def test_default_render_has_every_production_object() -> None:
    kinds = sorted(d["kind"] for d in _render())
    assert kinds == sorted(
        [
            "ConfigMap",
            "Deployment",
            "HorizontalPodAutoscaler",
            "Job",
            "NetworkPolicy",
            "Pod",  # helm test
            "PodDisruptionBudget",
            "Service",
            "ServiceAccount",
        ]
    )


@needs_helm
def test_pods_are_hardened() -> None:
    docs = _render()
    for kind in ("Deployment", "Job"):
        pod = _one(docs, kind)["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["runAsUser"] == 10001
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        for c in pod["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
            assert "limits" in c["resources"] and "requests" in c["resources"]
            assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]


@needs_helm
def test_probes_use_health_and_ready() -> None:
    c = _one(_render(), "Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert c["livenessProbe"]["httpGet"]["path"] == "/health"
    assert c["startupProbe"]["httpGet"]["path"] == "/health"
    assert c["readinessProbe"]["httpGet"]["path"] == "/ready"


@needs_helm
def test_secrets_reach_pods_only_as_secret_key_refs() -> None:
    docs = _render(
        "--set", "metrics.tokenSecret.name=scruple-metrics", "--set", "heldoutSalt.secret.name=s"
    )
    secret_env = {"SCRUPLE_DATABASE_PASSWORD", "SCRUPLE_METRICS_TOKEN", "SCRUPLE_HELDOUT_SALT"}
    for kind in ("Deployment", "Job"):
        env = _one(docs, kind)["spec"]["template"]["spec"]["containers"][0]["env"]
        by_name = {e["name"]: e for e in env}
        assert secret_env <= set(by_name)
        for name in secret_env:
            assert "value" not in by_name[name] and "secretKeyRef" in by_name[name]["valueFrom"]
    cm = _one(docs, "ConfigMap")["data"]
    assert not secret_env & set(cm)
    assert "Secret" not in {d["kind"] for d in docs}  # the chart never creates one


@needs_helm
def test_migration_job_is_a_pre_upgrade_hook() -> None:
    docs = _render()
    job = _one(docs, "Job")
    ann = job["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "pre-install,pre-upgrade"
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert "python -m scruple.storage.migrate" in c["args"][0]
    # Inline env (a pre-install hook runs before the ConfigMap exists).
    assert "envFrom" not in c
    assert {"name": "SCRUPLE_MIGRATE_ON_START", "value": "false"} in c["env"]
    cm = _one(docs, "ConfigMap")["data"]
    assert {e["name"]: e.get("value") for e in c["env"] if "value" in e} == cm


@needs_helm
def test_service_pdb_and_policy_select_only_api_pods() -> None:
    docs = _render()
    api = _one(docs, "Deployment")["spec"]["template"]["metadata"]["labels"]
    selectors = [
        _one(docs, "Service")["spec"]["selector"],
        _one(docs, "PodDisruptionBudget")["spec"]["selector"]["matchLabels"],
        _one(docs, "NetworkPolicy")["spec"]["podSelector"]["matchLabels"],
        _one(docs, "Deployment")["spec"]["selector"]["matchLabels"],
    ]
    others = [
        _one(docs, "Job")["spec"]["template"]["metadata"]["labels"],
        _one(docs, "Pod")["metadata"]["labels"],
    ]
    for sel in selectors:
        assert sel.items() <= api.items()
        for labels in others:
            assert not sel.items() <= labels.items()


@needs_helm
def test_network_policy_limits_egress_to_dns_and_postgres() -> None:
    np = _one(_render(), "NetworkPolicy")["spec"]
    assert np["policyTypes"] == ["Ingress", "Egress"]
    ports = sorted(p["port"] for rule in np["egress"] for p in rule["ports"])
    assert ports == [53, 53, 5432]


@needs_helm
def test_optional_objects() -> None:
    docs = _render(
        "--set",
        "ingress.enabled=true",
        "--set",
        "metrics.serviceMonitor.enabled=true",
        "--set",
        "metrics.tokenSecret.name=mt",
        "--set",
        "autoscaling.enabled=false",
        "--set",
        "networkPolicy.enabled=false",
    )
    kinds = {d["kind"] for d in docs}
    assert {"Ingress", "ServiceMonitor"} <= kinds
    assert not {"HorizontalPodAutoscaler", "NetworkPolicy"} & kinds
    assert _one(docs, "Deployment")["spec"]["replicas"] == 2
    sm = _one(docs, "ServiceMonitor")["spec"]["endpoints"][0]
    assert sm["authorization"]["credentials"] == {"name": "mt", "key": "token"}


@needs_helm
def test_digest_wins_over_tag() -> None:
    digest = "sha256:" + "a" * 64
    c = _one(_render("--set", f"image.digest={digest}", "--set", "image.tag=9.9.9"), "Deployment")
    image = c["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"ghcr.io/thedhruvhegde/scruple-engine@{digest}"


@needs_helm
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "database.passwordSecret.name is required"),
        (
            [*REQUIRED, "--set", "database.url=postgresql://u:pw@db/x"],
            "must not contain a password",
        ),
        ([*REQUIRED, "--set", "database.url=/data/scruple.db"], "must be a postgresql:// URL"),
        ([*REQUIRED, "--set", "config.extraEnv.SCRUPLE_API_TOKEN=x"], "looks like a secret"),
    ],
)
def test_render_refuses_unsafe_values(args: list[str], message: str) -> None:
    out = _helm(*args)
    assert out.returncode != 0 and message in out.stderr


@needs_helm
@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_kubeconform_strict() -> None:
    out = _helm(
        *REQUIRED, "--set", "ingress.enabled=true", "--set", "metrics.serviceMonitor.enabled=true"
    )
    assert out.returncode == 0, out.stderr
    check = subprocess.run(
        [
            "kubeconform",
            "-strict",
            "-summary",
            "-kubernetes-version",
            KUBE,
            "-skip",
            "ServiceMonitor",
            "-",
        ],
        input=out.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
