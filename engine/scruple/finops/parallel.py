"""Multiprocessing for bulk work: task generation, scripted rollouts and grading.

`pmap(fn, items, workers)` maps a top-level (picklable) function over items in a
process pool, preserving order, in chunks. `workers <= 1` runs in-process (no pool),
which is also what tests use. Episodes are pure functions of (seed, case type, calls),
so results do not depend on the number of workers (a test pins it).

`bench` measures throughput on this machine: scripted episodes per second and
grades per second, serial and parallel.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from scruple.finops.envs import get_env
from scruple.finops.policies import run_policy


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def _ctx() -> Any:
    """forkserver where available (fork from a threaded process can deadlock), with
    the suite preloaded so workers start warm; `SCRUPLE_MP_START` overrides."""
    methods = mp.get_all_start_methods()
    method = os.environ.get("SCRUPLE_MP_START") or (
        "forkserver" if "forkserver" in methods else "spawn"
    )
    ctx = mp.get_context(method)
    if method == "forkserver":
        ctx.set_forkserver_preload(["scruple.finops.parallel", "scruple.finops.exploits"])
    return ctx


def pmap[T, R](
    fn: Callable[[T], R], items: Sequence[T], *, workers: int = 1, chunksize: int | None = None
) -> list[R]:
    if workers <= 1 or len(items) < 2:
        return [fn(x) for x in items]
    workers = min(workers, len(items))
    cs = chunksize or max(1, len(items) // (workers * 8))
    with _ctx().Pool(workers) as pool:
        out: list[R] = pool.map(fn, items, chunksize=cs)
        return out


# --- scripted rollouts --------------------------------------------------------------


def play_row(item: tuple[str, str, int, str | None, tuple[str, ...]]) -> dict[str, Any]:
    """One scripted episode, scored by each named verifier: a flat row."""
    env, policy, seed, ct, verifiers = item
    e = get_env(env)
    ep = run_policy(e.episode_cls, e.policies()[policy], seed, ct)
    rec = ep.record()
    row: dict[str, Any] = {
        "env": env,
        "policy": policy,
        "seed": seed,
        "case_type": ct,
        "calls": ep.tool_calls,
        "lapsed": bool(rec.lapsed),
    }
    for vid in verifiers:
        card = e.verifier(vid).score(rec)
        row[vid] = {
            "reward": round(card.reward, 6),
            "gate": card.gate,
            "hard": [c.id for c in card.checks if c.hard and not c.passed],
            "soft": {c.id: round(c.score, 4) for c in card.checks if not c.hard},
        }
    return row


def _grade_item(item: tuple[str, str]) -> str:
    from scruple.finops.tasks import TaskSpec, Trajectory, grade_safe

    spec_json, traj_json = item
    spec = TaskSpec.model_validate_json(spec_json)
    return grade_safe(spec, Trajectory.model_validate_json(traj_json)).model_dump_json()


def grade_many(pairs: Sequence[tuple[str, str]], *, workers: int = 1) -> list[str]:
    """Grades (JSON) for (spec JSON, trajectory JSON) pairs."""
    return pmap(_grade_item, pairs, workers=workers)


def bench(
    env: str, n: int = 200, *, policy: str = "honest", workers: int | None = None, seed0: int = 0
) -> dict[str, Any]:
    """Episodes/s for a scripted policy and grades/s for replaying its trajectories,
    serial and with `workers` processes."""
    from scruple.finops.difficulty import curriculum
    from scruple.finops.tasks import build_task, rollout

    w = workers or default_workers()
    e = get_env(env)
    vid = (e.default_verifier,)
    items = [(env, policy, seed0 + i, curriculum(env, (i % 11) / 10), vid) for i in range(n)]
    t = time.perf_counter()
    pmap(play_row, items, workers=1)
    serial = n / (time.perf_counter() - t)
    t = time.perf_counter()
    pmap(play_row, items, workers=w)
    par = n / (time.perf_counter() - t)
    specs = [build_task(env, s, ct, "bench", measure=False) for _, _, s, ct, _ in items]
    pairs = [(s.model_dump_json(), rollout(s, policy).model_dump_json()) for s in specs]
    t = time.perf_counter()
    grade_many(pairs, workers=1)
    g_serial = n / (time.perf_counter() - t)
    t = time.perf_counter()
    grade_many(pairs, workers=w)
    g_par = n / (time.perf_counter() - t)
    return {
        "env": env,
        "policy": policy,
        "n": n,
        "workers": w,
        "cpus": os.cpu_count(),
        "episodes_per_s_serial": round(serial, 1),
        "episodes_per_s_parallel": round(par, 1),
        "grades_per_s_serial": round(g_serial, 1),
        "grades_per_s_parallel": round(g_par, 1),
    }


__all__ = ["bench", "default_workers", "grade_many", "play_row", "pmap"]
