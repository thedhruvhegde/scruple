"""Dispute generator throughput on this machine: `uv run python scripts/bench_sim.py`.

Synthetic data. Prints cases per second (best of `--repeats` runs) for the serial
generator (`generate_cases`) under three mixes, and for the sharded parallel generator
(`generate_sharded`) at 1, 2 and 4 worker processes, both returning every case to
the calling process ("sharded") and keeping each shard in the worker that made it
("in-worker", as a pipeline that consumes cases where they are generated would).
Local wall-clock measurements, not performance claims; pasted into docs/PERFORMANCE.md.
"""

from __future__ import annotations

import argparse
import os
import platform
import time
from collections.abc import Callable

from scruple.sim.difficulty import difficulty_mix
from scruple.sim.disputes import DEFAULT_MIX, FULL_MIX, CaseMix, generate_cases

MIXES: dict[str, CaseMix] = {
    "default": DEFAULT_MIX,
    "full (d=0.5)": FULL_MIX,
    "hard (d=1.0)": difficulty_mix(1.0),
}


def _count_shard(args: tuple[int, int, int]) -> int:
    """Generate one shard in a worker and keep it there (consumer-side generation)."""
    from scruple.sim.disputes import shard_seed

    seed, k, n = args
    return len(generate_cases(shard_seed(seed, k), n, FULL_MIX, id_prefix=f"{seed}s{k}"))


def local(seed: int, n: int, workers: int, shard: int = 2_000) -> int:
    from concurrent.futures import ProcessPoolExecutor

    jobs = [(seed, k, min(shard, n - k * shard)) for k in range((n + shard - 1) // shard)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(_count_shard, jobs))


def best(fn: Callable[[], object], repeats: int) -> float:
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--workers", type=int, nargs="*", default=[1, 2, 4])
    args = p.parse_args()
    n = args.cases
    print(
        f"scruple sim bench (synthetic, seed {args.seed}, {n:,} cases, best of {args.repeats}, "
        f"python {platform.python_version()}, {os.cpu_count()} cpus)"
    )
    generate_cases(args.seed, 500, FULL_MIX)  # warm caches and imports
    for name, mix in MIXES.items():
        dt = best(lambda mix=mix: generate_cases(args.seed, n, mix), args.repeats)
        print(f"serial   {name:14} {dt * 1000:9.1f} ms  {n / dt:>10,.0f} cases/s")
    try:
        from scruple.sim.disputes import generate_sharded
    except ImportError:  # pragma: no cover - before the sharded generator existed
        return 0
    for w in args.workers:
        dt = best(lambda w=w: generate_sharded(args.seed, n, FULL_MIX, workers=w), args.repeats)
        print(f"sharded  full, {w} worker(s) {dt * 1000:9.1f} ms  {n / dt:>10,.0f} cases/s")
    for w in args.workers:
        dt = best(lambda w=w: local(args.seed, 4 * n, w), args.repeats)
        print(
            f"in-worker full, {w} worker(s) {dt * 1000:9.1f} ms  {4 * n / dt:>10,.0f} cases/s"
            f"  ({4 * n:,} cases kept in the workers)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
