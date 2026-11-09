"""Cross-process and cross-backend determinism (roadmap 168).

Same seed, same outputs:
- across Python hash seeds: `tests.determinism_probe` digests every golden episode,
  a CI scorecard, an evidence pack and sharded generation in fresh interpreters
  with different PYTHONHASHSEED values (string hashing changes set and dict-of-set
  iteration order, the classic source of run-to-run drift);
- across storage backends: the gate's whole promotion and approval traces (audit
  chain, decisions, registry, head hash) are identical on SQLite in memory, SQLite
  on disk and PostgreSQL.

Synthetic data only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from scruple.storage import SQLiteStorage, Storage
from tests.determinism_probe import digests
from tests.golden.episodes import TRACES, approvals, dumps, promotion
from tests.golden.regen import path_for

ROOT = Path(__file__).resolve().parents[1]
QUICK_SEEDS = ("0", "random")
ALL_SEEDS = ("0", "1", "2", "4242", "123456789", "random")


def _probes(hash_seeds: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Run the probe once per hash seed, all at the same time."""
    procs = {
        s: subprocess.Popen(
            [sys.executable, "-m", "tests.determinism_probe"],
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": s},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for s in hash_seeds
    }
    out: dict[str, dict[str, str]] = {}
    for s, p in procs.items():
        stdout, stderr = p.communicate(timeout=600)
        assert p.returncode == 0, stderr[-2000:]
        out[s] = json.loads(stdout)
    return out


def _check(probes: dict[str, dict[str, str]]) -> None:
    from scruple.hashing import sha256_hex

    first = next(iter(probes.values()))
    assert set(first) >= set(TRACES)
    for seed, got in probes.items():
        diff = sorted(k for k in first if got.get(k) != first[k])
        assert not diff, f"PYTHONHASHSEED={seed} changed {diff}"
    for name in TRACES:  # and every subprocess agrees with the golden files
        assert first[name] == sha256_hex(path_for(name).read_text(encoding="utf-8")), name


def test_outputs_do_not_depend_on_the_hash_seed() -> None:
    probes = _probes(QUICK_SEEDS)
    _check(probes)
    assert digests() == probes["0"]  # nor on whether it runs in this process


@pytest.mark.slow
def test_outputs_do_not_depend_on_any_of_many_hash_seeds() -> None:
    _check(_probes(ALL_SEEDS))


# --------------------------------------------------------------------- backends
BACKENDS = ["sqlite-memory", "sqlite-file", pytest.param("postgres", marks=pytest.mark.postgres)]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "postgres":
        import psycopg

        from scruple.storage.postgres import PostgresStorage

        url: str = request.getfixturevalue("postgres_url")
        schema = f"det_{uuid.uuid4().hex[:12]}"
        s: Storage = PostgresStorage(url, schema=schema)
        yield s
        s.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
        return
    s = SQLiteStorage(":memory:" if request.param == "sqlite-memory" else str(tmp_path / "d.db"))
    yield s
    s.close()


def test_promotion_trace_is_identical_on_every_backend(backend: Storage) -> None:
    got = dumps(promotion(1, backend))
    assert got == path_for("gate_promotion_seed1").read_text(encoding="utf-8")


def test_approval_trace_is_identical_on_every_backend(backend: Storage) -> None:
    got = dumps(approvals(backend))
    assert got == path_for("gate_approvals").read_text(encoding="utf-8")
