from __future__ import annotations

import json
from pathlib import Path

import pytest

from scruple.cli.main import main
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID


def test_demo_runs_and_tells_the_story(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for needle in (
        "PROMOTE",
        "CANARY_PASSED",
        "BLOCK",
        "ACTIVATED",
        "ROLLED_BACK",
        "TAMPERING DETECTED",
        "INTEGRITY OK",
        "7/7 attacks stopped",
        "# Evidence report dec-00003: BLOCKED",
    ):
        assert needle in out
    assert len(list((tmp_path / "reports").glob("*.md"))) == 6


def test_demo_output_is_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["demo", "--out", str(tmp_path / "a"), "--quiet"])
    a = capsys.readouterr().out.replace(str(tmp_path / "a"), "X")
    main(["demo", "--out", str(tmp_path / "b"), "--quiet"])
    b = capsys.readouterr().out.replace(str(tmp_path / "b"), "X")
    assert a == b


def test_cli_workflow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "g.db")
    f = tmp_path / "rule.json"
    f.write_text(
        json.dumps(
            {
                "rule_id": GOOD_RULE_ID,
                "spec": GOOD_RULE.model_dump(mode="json"),
                "owner": "ops",
                "provenance": {"agent_id": "agent-1"},
            }
        )
    )
    assert main(["propose", str(f), "--db", db]) == 0
    assert main(["evaluate", GOOD_RULE_ID, "1", "--db", db]) == 0
    assert (
        main(["approve", GOOD_RULE_ID, "1", "--approver", "model-risk@demo.bank", "--db", db]) == 0
    )
    assert main(["rules", "--db", db]) == 0
    assert main(["report", "dec-00001", "--db", db]) == 0
    assert main(["assurance", "--db", db]) == 0
    assert (
        main(
            [
                "rollback",
                GOOD_RULE_ID,
                "--actor",
                "model-risk@demo.bank",
                "--reason",
                "test",
                "--db",
                db,
            ]
        )
        == 0
    )
    assert main(["audit", "verify", "--db", db]) == 0
    assert main(["report", "dec-99999", "--db", db]) == 1
    assert main(["approve", GOOD_RULE_ID, "1", "--approver", "mallory", "--db", db]) == 2
    out = capsys.readouterr().out
    assert "INTEGRITY OK" in out


def test_audit_verify_fails_on_bad_anchor(tmp_path: Path) -> None:
    db = str(tmp_path / "g.db")
    assert main(["audit", "verify", "--db", db]) == 0
    assert main(["audit", "verify", "--db", db, "--expected-head", "f" * 64]) == 1


def test_audit_verify_never_writes(tmp_path: Path) -> None:
    """Regression (found while writing tests/test_e2e_processes.py): `audit verify`
    imported the baseline rules first, so verifying a database appended six audit
    entries to the very evidence it was checking."""
    from scruple.storage import SQLiteStorage

    db = str(tmp_path / "g.db")
    assert main(["audit", "verify", "--db", db]) == 0
    s = SQLiteStorage(db)
    assert s.list_audit() == [] and s.list_rule_versions() == []
    s.close()
    assert main(["rules", "--db", db]) == 0  # ordinary commands still bootstrap
    s = SQLiteStorage(db)
    head = s.list_audit()[-1].entry_hash
    n = len(s.list_audit())
    s.close()
    assert main(["audit", "verify", "--db", db, "--expected-head", head]) == 0
    s = SQLiteStorage(db)
    assert len(s.list_audit()) == n
    s.close()
