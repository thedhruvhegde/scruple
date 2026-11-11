"""`scruple shadow imported record|observe|summary|history` over the persisted store."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.cli.main import main
from scruple.gate.shadow_imported import ImportedOutcome, ImportedShadowSummary
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from tests.ci_support import PROV
from tests.test_gate_shadow_imported import decisions, synthetic

ROWS = synthetic(30, seed=11)
BATCH = decisions(ROWS, as_opened=True)
OUTCOMES = [ImportedOutcome(case=c, movements=mv) for c, mv in ROWS]


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "gate.db")
    g = AgentGate.open(path)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.storage.close()
    return path


def write(tmp_path: Path, name: str, key: str, items: list[Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps({key: [json.loads(i.model_dump_json()) for i in items]}))
    return str(path)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def version(db: str) -> str:
    g = AgentGate.open(db)
    v = g.registry.versions(GOOD_RULE_ID)[-1].version
    g.storage.close()
    return str(v)


def test_record_observe_summary_history(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = version(db)
    base = [GOOD_RULE_ID, v, "--db", db]
    first = write(tmp_path, "d1.json", "decisions", BATCH[:20])
    second = write(tmp_path, "d2.json", "decisions", BATCH[20:])
    outs = write(tmp_path, "o.json", "outcomes", OUTCOMES[:15])

    code, out = run(["shadow", "imported", "record", *base, first, "--actor", "ops"], capsys)
    assert code == 0 and json.loads(out)["recorded"] == 20
    code, out = run(["shadow", "imported", "record", *base, second, "--actor", "ops"], capsys)
    assert code == 0 and json.loads(out)["total_recorded"] == len(BATCH)
    code, out = run(["shadow", "imported", "observe", *base, outs, "--actor", "ops"], capsys)
    assert code == 0 and json.loads(out)["updated"] == 15

    code, out = run(["shadow", "imported", "summary", *base, "--json"], capsys)
    summary = ImportedShadowSummary.model_validate_json(out)
    assert code == 0 and summary.decisions == len(BATCH) and summary.resolved > 0
    assert summary.data == "real (imported)"

    code, text = run(["shadow", "imported", "summary", *base], capsys)
    assert code == 0 and f"decisions {len(BATCH)}" in text and "observed" in text

    code, out = run(["shadow", "imported", "history", *base, "--json"], capsys)
    hist = json.loads(out)
    assert [h["seq"] for h in hist] == [1, 2, 3]
    assert hist[-1]["summary"] == summary.model_dump(mode="json")
    assert hist[0]["summary"]["decisions"] == 20 and hist[0]["summary"]["resolved"] == 0

    code, text = run(["shadow", "imported", "history", *base], capsys)
    assert code == 0 and len(text.strip().splitlines()) == 3


def test_cli_and_api_read_the_same_store(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = version(db)
    path = write(tmp_path, "d.json", "decisions", BATCH)
    argv = ["shadow", "imported", "record", GOOD_RULE_ID, v, "--db", db, path]
    assert main([*argv, "--actor", "production"]) == 0
    capsys.readouterr()
    g = AgentGate.open(db)
    c = TestClient(
        create_app(g, auth=Authenticator(dev_actor_header=True)),
        headers={"X-Actor": "auditor"},
    )
    url = f"/shadow/rules/{GOOD_RULE_ID}/versions/{v}/imported"
    out = {"outcomes": [{"case": json.loads(o.case.model_dump_json())} for o in OUTCOMES]}
    assert c.post(f"{url}/outcomes", json=out, headers={"X-Actor": "production"}).is_success
    api = c.get(url).json()
    g.storage.close()
    code, cli = run(
        ["shadow", "imported", "summary", GOOD_RULE_ID, v, "--db", db, "--json"], capsys
    )
    assert code == 0 and json.loads(cli) == api


def test_refused_batch_changes_nothing(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = version(db)
    path = write(tmp_path, "d.json", "decisions", BATCH[:5])
    argv = ["shadow", "imported", "record", GOOD_RULE_ID, v, "--db", db, path, "--actor", "ops"]
    assert main(argv) == 0
    assert main(argv) == 2  # the same cases again: refused
    err = capsys.readouterr().err
    assert "error:" in err
    code, out = run(
        ["shadow", "imported", "history", GOOD_RULE_ID, v, "--db", db, "--json"], capsys
    )
    assert code == 0 and len(json.loads(out)) == 1


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"decisions": []}',
        '{"decisions": [{"case": {}}]}',
        '{"outcomes": []}',
    ],
)
def test_malformed_batches_exit_2(
    db: str, tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body)
    argv = ["shadow", "imported", "record", GOOD_RULE_ID, version(db), "--db", db, str(path)]
    assert main([*argv, "--actor", "ops"]) == 2
    assert "error:" in capsys.readouterr().err


def test_unknown_rule_exits_2(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["shadow", "imported", "summary", "nope", "1", "--db", db]) == 2
    assert "error:" in capsys.readouterr().err


def test_reads_do_not_write(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    import sqlite3

    def rows() -> int:
        with sqlite3.connect(db) as conn:
            tables = ("audit_events", "rule_versions", "imported_shadow_events")
            return sum(int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in tables)

    before = rows()
    v = version(db)
    assert main(["shadow", "imported", "summary", GOOD_RULE_ID, v, "--db", db]) == 0
    assert main(["shadow", "imported", "history", GOOD_RULE_ID, v, "--db", db]) == 0
    assert "no batches recorded" in capsys.readouterr().out
    assert rows() == before


def test_record_from_stdin(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.dumps({"decisions": [json.loads(d.model_dump_json()) for d in BATCH[:3]]})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    argv = ["shadow", "imported", "record", GOOD_RULE_ID, version(db), "--db", db, "-"]
    code, out = run([*argv, "--actor", "ops"], capsys)
    assert code == 0 and json.loads(out)["recorded"] == 3
