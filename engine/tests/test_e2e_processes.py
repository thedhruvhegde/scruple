"""End to end in real processes (roadmap 173): pull request to canary to rollback.

Nothing here runs in the test process except the HTTP client and file reads:

1. `scruple ci run` (a subprocess) gates a candidate from a repository checkout into
   a temporary SQLite file: the bad rule is blocked, the good one reaches canary;
2. `scruple serve` (another subprocess, a real uvicorn on a free port) serves that
   file: the owner cannot approve, the approver can, the report renders;
3. `scruple rollback` (a CLI subprocess, while the server is still running) rolls the
   rule back, and the server sees it at once;
4. `scruple ci evidence` exports the evidence pack (JSON and PDF), which verifies on
   its own and records the whole story;
5. `scruple audit verify` passes against the head the server reported, and fails
   once the database file is tampered with.

Synthetic data only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from scruple.ci.config import CandidateFile
from scruple.ci.evidence import load_pack
from scruple.ci.scorecard import Verdict, load_scorecard
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV, write_repo

ROOT = Path(__file__).resolve().parents[1]
# Fast scrypt for test keys; the server process reads the same setting.
ENV = {**os.environ, "PYTHONHASHSEED": "random", "SCRUPLE_AUTH_SCRYPT_N": "1024"}


def scruple(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "scruple.cli.main", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        env=ENV,
    )
    if check:
        assert proc.returncode == 0, f"scruple {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def key(db: Path, subject: str, *scopes: str) -> dict[str, str]:
    """A real API key made by `scruple keys create`, as request headers."""
    flags = [f"--scope={s}" for s in scopes]
    out = scruple("keys", "create", "--db", str(db), "--subject", subject, *flags)
    return {"X-API-Key": out.stdout.strip()}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A gated database and a real `scruple serve` process on it."""
    db = tmp_path / "gate.db"
    bad = write_repo(
        tmp_path / "pr-bad",
        CandidateFile(rule_id=BAD_RULE_ID, spec=BAD_RULE, owner="ops-team", provenance=PROV),
    )
    good = write_repo(
        tmp_path / "pr-good",
        CandidateFile(rule_id=GOOD_RULE_ID, spec=GOOD_RULE, owner="ops-team", provenance=PROV),
    )
    blocked = scruple("ci", "run", "--config", str(bad), "--db", str(db), check=False)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "BLOCK" in blocked.stdout
    scruple("ci", "run", "--config", str(good), "--db", str(db))
    card = load_scorecard((tmp_path / "pr-good" / "out" / "scorecard.json").read_text())
    assert card.verdict is Verdict.PASS

    port = _free_port()
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "scruple.cli.main", "serve", "--db", str(db), "--port", str(port)],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=ENV,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert proc.poll() is None, (tmp_path / "server.log").read_text()
            assert time.monotonic() < deadline, "server did not come up"
            time.sleep(0.1)
        yield base, db
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        log.close()


def test_pull_request_to_canary_to_rollback_across_processes(
    server: tuple[str, Path], tmp_path: Path
) -> None:
    base, db = server
    approver = key(db, "model-risk@demo.bank", "read", "approve")
    owner = key(db, "ops-team", "read", "approve")  # holds approve, yet owns the rule
    reader = key(db, "auditor", "read")
    with httpx.Client(base_url=base, timeout=30, headers=reader) as http:
        assert http.get("/rules", headers={"X-API-Key": ""}).status_code == 401
        versions = {(r["rule_id"], r["version"]): r["status"] for r in http.get("/rules").json()}
        assert versions[(BAD_RULE_ID, 1)] == "rejected"
        assert versions[(GOOD_RULE_ID, 1)] == "canary"

        # the owner may not approve their own rule; the approver may
        r = http.post(f"/rules/{GOOD_RULE_ID}/versions/1/approve", headers=owner)
        assert r.status_code == 403, r.text
        r = http.post(f"/rules/{GOOD_RULE_ID}/versions/1/approve", headers=approver)
        assert r.status_code == 200, r.text
        activation = r.json()
        assert activation["outcome"] == "activated"
        report = http.get(activation["report_url"])
        assert report.status_code == 200 and report.text.startswith("# Evidence report")
        assert http.get(f"/rules/{GOOD_RULE_ID}").json()[0]["status"] == "active"

        # a different process rolls it back while the server keeps running
        out = scruple(
            "rollback",
            GOOD_RULE_ID,
            "--actor",
            "model-risk@demo.bank",
            "--reason",
            "e2e drill",
            "--db",
            str(db),
        )
        assert "Rolled back by model-risk@demo.bank: e2e drill" in out.stdout
        assert http.get(f"/rules/{GOOD_RULE_ID}").json()[0]["status"] == "retired"
        outcomes = [d["outcome"] for d in http.get("/decisions").json()]
        assert outcomes[-2:] == ["activated", "rolled_back"]

        verify = http.get("/audit/verify").json()
        assert verify["ok"] is True
        head = verify["chain"]["head_hash"]

    # the evidence pack, exported by yet another process
    evidence = tmp_path / "evidence"
    scruple("ci", "evidence", "--db", str(db), GOOD_RULE_ID, "1", "--out", str(evidence))
    stem = evidence / f"evidence-{GOOD_RULE_ID}-v1"
    pdf = stem.with_suffix(".pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and len(pdf) > 1_000
    pack, digest = load_pack(stem.with_suffix(".json").read_text())
    assert digest == pack.pack_sha256
    assert pack.audit.proof.ok and pack.audit.proof.chain_ok
    stages = [d.stage.value for d in pack.decisions]
    assert stages[:2] == ["shadow", "canary"] and "rollback" in stages

    # the audit trail verifies against the head the server reported...
    ok = scruple("audit", "verify", "--db", str(db), "--expected-head", head)
    assert "INTEGRITY OK" in ok.stdout

    # ...and stops verifying once someone edits the file behind its back
    s = SQLiteStorage(str(db))
    s.unsafe_execute("DROP TRIGGER rule_versions_immutable")
    s.unsafe_execute(
        f"UPDATE rule_versions SET status='active' WHERE rule_id='{BAD_RULE_ID}' AND version=1"
    )
    s.close()
    bad = scruple("audit", "verify", "--db", str(db), "--expected-head", head, check=False)
    assert bad.returncode == 1
    assert f"{BAD_RULE_ID}@v1: status active but audit log says rejected" in bad.stdout
    assert json.loads(stem.with_suffix(".json").read_text())["pack_sha256"] == digest
