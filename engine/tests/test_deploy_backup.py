"""Backup and restore runbook scripts, run for real against the throwaway
PostgreSQL cluster the storage tests use (docs/DEPLOY.md, "Backups and restore")."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import migrate as m

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "deploy" / "scripts" / "backup.sh"
RESTORE = ROOT / "deploy" / "scripts" / "restore.sh"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
        reason="pg_dump/pg_restore not installed",
    ),
]


def _with_db(url: str, db: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


@pytest.fixture
def make_db(postgres_url: str) -> Iterator[Callable[[], str]]:
    made: list[str] = []

    def make() -> str:
        name = f"bk_{uuid.uuid4().hex[:16]}"
        with psycopg.connect(postgres_url, autocommit=True) as c:
            c.execute(f"CREATE DATABASE {name}")
        made.append(name)
        return _with_db(postgres_url, name)

    yield make
    with psycopg.connect(postgres_url, autocommit=True) as c:
        for name in made:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _env() -> dict[str, str]:
    bindir = str(Path(sys.executable).parent)
    return {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}


def _run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, env=_env(), check=False
    )


def _populated(url: str) -> str:
    """Migrate, import the baseline, record decisions; return the audit head hash."""
    m.upgrade(url)
    g = AgentGate.open(url, SteppingClock(), GateConfig(n_cases=500, bootstrap_resamples=100))
    try:
        g.bootstrap_baseline()
        g.propose(GOOD_RULE_ID, GOOD_RULE, "payments-ops", Provenance(agent_id="agent-01"))
        g.evaluate(GOOD_RULE_ID, 1, "agent-01")
        return g.audit.verify().head_hash
    finally:
        g.storage.close()


def test_backup_then_restore_round_trips_and_verifies(
    make_db: Callable[[], str], tmp_path: Path
) -> None:
    src, dst = make_db(), make_db()
    head = _populated(src)
    prefix = tmp_path / "scruple-backup"
    b = _run(BACKUP, src, prefix)
    assert b.returncode == 0, b.stderr
    assert {p.name for p in tmp_path.iterdir()} == {
        "scruple-backup.dump",
        "scruple-backup.sha256",
        "scruple-backup.head",
    }
    assert (tmp_path / "scruple-backup.head").read_text().split()[1] == head

    r = _run(RESTORE, prefix, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "INTEGRITY OK" in r.stdout and "restore: OK" in r.stdout

    g = AgentGate.open(dst)
    try:
        rep = g.verify_integrity(head)
        assert rep.ok, rep.problems
        assert len(g.decisions()) == 2
    finally:
        g.storage.close()


def test_backup_refuses_a_password_in_the_url(tmp_path: Path) -> None:
    r = _run(BACKUP, "postgresql://u:hunter2@127.0.0.1/db", tmp_path / "x")
    assert r.returncode == 2 and "PGPASSWORD" in r.stderr
    assert not list(tmp_path.iterdir())


def test_restore_refuses_a_database_that_is_not_empty(
    make_db: Callable[[], str], tmp_path: Path
) -> None:
    src, dst = make_db(), make_db()
    _populated(src)
    m.upgrade(dst)
    prefix = tmp_path / "b"
    assert _run(BACKUP, src, prefix).returncode == 0
    r = _run(RESTORE, prefix, dst)
    assert r.returncode == 1 and "empty database" in r.stderr


def test_restore_refuses_a_corrupted_dump(make_db: Callable[[], str], tmp_path: Path) -> None:
    src, dst = make_db(), make_db()
    _populated(src)
    prefix = tmp_path / "b"
    assert _run(BACKUP, src, prefix).returncode == 0
    dump = tmp_path / "b.dump"
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    dump.write_bytes(bytes(data))
    r = _run(RESTORE, prefix, dst)
    assert r.returncode == 1 and "checksum mismatch" in r.stderr


def test_restore_catches_a_dump_that_lost_the_recorded_head(
    make_db: Callable[[], str], tmp_path: Path
) -> None:
    src, dst = make_db(), make_db()
    _populated(src)
    prefix = tmp_path / "b"
    assert _run(BACKUP, src, prefix).returncode == 0
    seq, _ = (tmp_path / "b.head").read_text().split()
    (tmp_path / "b.head").write_text(f"{seq} {'0' * 64}\n")  # anchor that is not in the dump
    r = _run(RESTORE, prefix, dst)
    assert r.returncode == 1 and "does not match the head" in r.stderr
