"""Static checks on the AWS Terraform skeleton (deploy/terraform/aws).

The module is UNTESTED against a real AWS account; these tests only pin the safety
properties of the text. `terraform fmt -check` runs when terraform is on PATH
(`terraform validate` needs the AWS provider, so it is a manual step, docs/DEPLOY.md).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TF = ROOT / "deploy" / "terraform" / "aws"
SOURCES = {p.name: p.read_text() for p in sorted(TF.glob("*.tf"))}
ALL = "\n".join(SOURCES.values())


def test_module_says_it_is_untested() -> None:
    assert "UNTESTED AGAINST A REAL AWS ACCOUNT" in SOURCES["versions.tf"]


def test_no_secret_inputs_or_values() -> None:
    variables = re.findall(r'^variable "([^"]+)"', ALL, re.M)
    assert variables
    for name in variables:
        assert not re.search(r"password|token|salt|secret_value", name), name
    assert "random_password" not in ALL
    assert "secret_string" not in ALL  # values are written out of band, never in state
    assert "manage_master_user_password = true" in SOURCES["database.tf"]


def test_database_is_private_encrypted_and_backed_up() -> None:
    db = SOURCES["database.tf"]
    for line in (
        "publicly_accessible    = false",
        "storage_encrypted     = true",
        "backup_retention_period    = var.db_backup_retention_days",
        'deletion_protection       = var.environment == "prod"',
    ):
        assert line in db
    assert '"rds.force_ssl"' in db


def test_tasks_are_hardened_and_get_secrets_by_reference() -> None:
    main = SOURCES["main.tf"]
    assert 'user                   = "10001:10001"' in main
    assert "readonlyRootFilesystem = true" in main
    assert 'drop = ["ALL"]' in main
    assert re.findall(r'name = "(SCRUPLE_[A-Z_]+)", valueFrom', main) == [
        "SCRUPLE_DATABASE_PASSWORD",
        "SCRUPLE_METRICS_TOKEN",
        "SCRUPLE_HELDOUT_SALT",
    ]
    assert '"SCRUPLE_MIGRATE_ON_START", value = "false"' in main


def test_image_must_be_a_digest() -> None:
    assert "@sha256:[0-9a-f]{64}$" in SOURCES["variables.tf"]


def test_load_balancer_hides_metrics_and_checks_readiness() -> None:
    alb = SOURCES["alb.tf"]
    assert 'values = ["/metrics", "/metrics/*"]' in alb
    assert 'path                = "/ready"' in alb
    assert 'protocol          = "HTTPS"' in alb


def test_nothing_is_open_by_default() -> None:
    assert re.search(r'variable "allowed_cidrs" \{[^}]*default     = \[\]', ALL, re.S)


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_terraform_fmt() -> None:
    out = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", str(TF)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
