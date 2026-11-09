"""Micro-benchmark of the hot paths on synthetic data: `make bench`.

Prints wall-clock throughput measured on this machine for one fixed seed. These are
local measurements of synthetic workloads, not performance claims.
"""

from __future__ import annotations

import argparse
import platform
import time
from collections.abc import Callable

from scruple.audit.log import AuditLog
from scruple.clock import SteppingClock
from scruple.sim import Simulator, generate_cases
from scruple.sim.library import baseline_policy
from scruple.storage import SQLiteStorage
from scruple.verifiers import verify_ledger


def timed[T](label: str, n: int, unit: str, fn: Callable[[], T]) -> T:
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"{label:28} {n:>8} {unit:8} {dt * 1000:9.1f} ms  {n / dt:>12,.0f} {unit}/s")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    n = args.cases
    print(f"scruple bench (synthetic data, seed {args.seed}, python {platform.python_version()})")
    cases = timed("generate cases", n, "cases", lambda: generate_cases(args.seed, n))
    policy = baseline_policy()
    run = timed("simulate baseline policy", n, "cases", lambda: Simulator(cases).run(policy))
    timed("verify ledger", len(run.journal), "entries", lambda: verify_ledger(cases, run))

    def audit_writes() -> None:
        log = AuditLog(SQLiteStorage(":memory:"), SteppingClock())
        for i in range(2_000):
            log.append("bench", "bench", {"i": i})
        assert log.verify().ok

    timed("audit append (sqlite mem)", 2_000, "entries", audit_writes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
