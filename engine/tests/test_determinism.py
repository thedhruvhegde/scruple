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
HASH_SEEDS = ("0", "1", "4242", "random")


def _probe(hash_seed: str) -> dict[str, str]:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    out = subprocess.run(
        [sys.executable, "-m", "tests.determinism_probe"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    result: dict[str, str] = json.loads(out.stdout)
    return result


@pytest.fixture(scope="module")
def probes() -> dict[str, dict[str, str]]:
    return {s: _probe(s) for s in HASH_SEEDS}


def test_outputs_do_not_depend_on_the_hash_seed(probes: dict[str, dict[str, str]]) -> None:
    first = probes[HASH_SEEDS[0]]
    assert set(first) >= set(TRACES)
    for seed, got in probes.items():
        diff = sorted(k for k in first if got.get(k) != first[k])
        assert not diff, f"PYTHONHASHSEED={seed} changed {diff}"


def test_subprocess_outputs_match_this_process(probes: dict[str, dict[str, str]]) -> None:
    assert digests() == probes[HASH_SEEDS[0]]


def test_subprocess_outputs_match_the_golden_files(probes: dict[str, dict[str, str]]) -> None:
    from scruple.hashing import sha256_hex

    for name in TRACES:
        want = sha256_hex(path_for(name).read_text(encoding="utf-8"))
        assert probes["random"][name] == want, name


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
