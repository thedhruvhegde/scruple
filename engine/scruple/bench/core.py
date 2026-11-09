"""Build, freeze, verify and score a benchmark version (174).

`build` draws `n` held-out cases for a version (needs the salt), runs every
reference policy in `scruple.agents.dispute_baselines` on them and returns a
`BenchFile`: the manifest, its hash, and the reference scores with intervals. That
file is what gets committed (results/bench/v<version>/manifest.json); the cases and
seeds never are.

`score_agent` regenerates the cases from the salt, checks every block digest against
the manifest (a wrong salt or a changed generator is refused, not silently scored),
and plays the agent. A prefix of the benchmark (`limit`) is allowed for quick runs;
it is checked block by block and labelled as a prefix.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from scruple.bench.metrics import Score, rows_of, score
from scruple.bench.runner import BLOCK, BUILTIN, Agent, BlockResult, Extra, load_agent, run_agent
from scruple.bench.spec import (
    DEFAULT_CASES,
    BenchCase,
    BenchManifest,
    CaseInfo,
    bench_cases,
    block_digests,
    check_salt,
    info_of,
    make_manifest,
    require_salt,
)
from scruple.sim.splits import SeedSpace
from scruple.version import package_version

REFERENCE_AGENTS: tuple[str, ...] = tuple(BUILTIN)  # evidence, always_accept, oracle


class BenchFile(BaseModel):
    """The committed, public part of a benchmark version."""

    model_config = ConfigDict(frozen=True)

    manifest: BenchManifest
    manifest_hash: str
    reference: dict[str, Score]
    note: str = (
        "Synthetic dispute episodes from the sealed held-out namespace. Only this file is "
        "published; the cases regenerate from the salt (SCRUPLE_HELDOUT_SALT) and the "
        "manifest. Intervals are 95% normal-approximation intervals over cases."
    )

    @classmethod
    def load(cls, path: str | Path) -> BenchFile:
        f = cls.model_validate_json(Path(path).read_text())
        if f.manifest.manifest_hash != f.manifest_hash:
            raise ValueError(f"{path}: manifest hash does not match its content")
        return f

    def dump(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2) + "\n")


def _infos(cases: Sequence[BenchCase]) -> list[CaseInfo]:
    return [info_of(c) for c in cases]


def case_infos(cases: Sequence[BenchCase], jobs: int = 1, chunk: int = BLOCK) -> list[CaseInfo]:
    parts = [list(cases[i : i + chunk]) for i in range(0, len(cases), chunk)]
    if jobs <= 1 or len(parts) <= 1:
        return [x for p in parts for x in _infos(p)]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("fork")) as pool:
        return [x for part in pool.map(_infos, parts) for x in part]


def build(
    space: SeedSpace,
    version: int,
    n: int = DEFAULT_CASES,
    *,
    jobs: int = 1,
    block: int = BLOCK,
    agents: Sequence[str] = REFERENCE_AGENTS,
) -> tuple[BenchFile, dict[str, list[BlockResult]]]:
    """The benchmark file for `version` and the reference runs it was scored from."""
    require_salt(space)
    cases = bench_cases(space, version, range(n))
    infos = case_infos(cases, jobs, block)
    manifest = make_manifest(space, version, cases, infos, block, package_version())
    runs = {a: run_agent(load_agent(a), cases, jobs=jobs, block=block) for a in agents}
    for name, res in runs.items():
        problems = check_blocks(manifest, res)
        if problems:  # pragma: no cover - the generator is deterministic
            raise RuntimeError(f"{name}: {problems[0]}")
    ref = {a: score(a, res) for a, res in runs.items()}
    return BenchFile(manifest=manifest, manifest_hash=manifest.manifest_hash, reference=ref), runs


def check_blocks(manifest: BenchManifest, results: Sequence[BlockResult]) -> list[str]:
    """Each played block's case digests against the manifest."""
    out: list[str] = []
    for b in results:
        k = b.start // manifest.block
        got = block_digests([r.digest for r in b.rows], manifest.block)
        want = manifest.block_digests[k : k + 1]
        if got != want:
            out.append(f"block {k} (cases {b.start}-{b.start + len(b.rows) - 1}) differs")
    return out


class ScoreRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_hash: str
    version: int
    prefix: bool  # scored on the first `n_cases` only
    seconds: float
    score: Score
    reference: dict[str, Score]


def cases_for(manifest: BenchManifest, space: SeedSpace, limit: int | None) -> list[BenchCase]:
    check_salt(space, manifest)
    n = manifest.n_cases if limit is None else min(limit, manifest.n_cases)
    if n < 1:
        raise ValueError("limit must be positive")
    if n < manifest.n_cases and n % manifest.block:
        n = min(manifest.n_cases, (n // manifest.block + 1) * manifest.block)
    return bench_cases(space, manifest.version, range(n))


def score_agent(
    bench: BenchFile,
    agent: str | Agent,
    space: SeedSpace,
    *,
    limit: int | None = None,
    jobs: int = 1,
    extra: Extra | None = None,
) -> tuple[ScoreRun, list[BlockResult]]:
    """Play `agent` on the benchmark (or its first `limit` cases, rounded up to a block)."""
    a = load_agent(agent) if isinstance(agent, str) else agent
    cases = cases_for(bench.manifest, space, limit)
    t0 = time.perf_counter()
    results = run_agent(a, cases, jobs=jobs, extra=extra, block=bench.manifest.block)
    problems = check_blocks(bench.manifest, results)
    if problems:
        raise ValueError(
            "the regenerated cases do not match the manifest "
            f"({problems[0]}): a different engine version or salt"
        )
    run = ScoreRun(
        manifest_hash=bench.manifest_hash,
        version=bench.manifest.version,
        prefix=len(cases) < bench.manifest.n_cases,
        seconds=round(time.perf_counter() - t0, 1),
        score=score(a.name, results),
        reference=bench.reference,
    )
    return run, results


def verify(bench: BenchFile, space: SeedSpace, *, jobs: int = 1) -> list[str]:
    """Regenerate every case and compare with the manifest (needs the salt)."""
    m = bench.manifest
    check_salt(space, m)
    cases = bench_cases(space, m.version, range(m.n_cases))
    infos = case_infos(cases, jobs, m.block)
    again = make_manifest(space, m.version, cases, infos, m.block, m.engine_version)
    out = []
    if again.cases_digest != m.cases_digest:
        out.append("cases digest differs")
    if again.mix != m.mix:
        out.append("mix differs")
    if again.seeds_commitment != m.seeds_commitment:
        out.append("seeds commitment differs")
    if again.mix_hash != m.mix_hash:
        out.append("generator mix differs")
    return out


__all__ = [
    "REFERENCE_AGENTS",
    "BenchFile",
    "ScoreRun",
    "build",
    "case_infos",
    "cases_for",
    "check_blocks",
    "rows_of",
    "score_agent",
    "verify",
]
