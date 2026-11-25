"""Commerce (Scruple Guard) evaluation on many seeds, with confidence intervals.

Plays the careful (honest) and obedient (hijackable) agents with the Guard enforcing
and in monitor mode (`noguard`: denials are logged, not enforced), on the seed's own
scenario mix and on each scenario alone, and reports gate pass rates, rewards, money
paid outside the mandate and per-check failures. Synthetic.

    uv run python scripts/exp_commerce_eval.py --n 3000 --jobs 4 \
        --md results/experiments/commerce_eval.md
"""

from __future__ import annotations

import argparse
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from scruple.agents.commerce_baselines import COMMERCE_BASELINES, run_commerce
from scruple.sim.commerce.models import Scenario

OFFSET = 100_000  # seeds used here: OFFSET + i (disjoint from the unit tests' small seeds)


def _play(job: tuple[str, str | None, int]) -> dict[str, Any]:
    policy, case_type, i = job
    ep = run_commerce(COMMERCE_BASELINES[policy], OFFSET + i, case_type)
    rec = ep.record()
    card = ep.score.scorecard if ep.score is not None else None
    assert card is not None
    from scruple.verifiers.commerce import _mandate

    return {
        "policy": policy,
        "case_type": case_type or "mix",
        "i": i,
        "gate": card.gate,
        "reward": card.reward,
        "hard": list(card.hard_failures),
        "failed": list(card.failed),
        "outside_mandate": not _mandate(rec)[0],
        "gross": rec.outcome.gross_captured_minor,
        "budget": rec.case.task.budget_minor,
        "units": rec.outcome.task_units,
        "expected": rec.outcome.expected_units,
        "scenarios": [s.value for s in rec.case.truth.scenarios],
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k / n:.1%} [{lo:.1%}, {hi:.1%}]" if n else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--per-scenario", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--md", type=Path, required=True)
    args = ap.parse_args()
    jobs: list[tuple[str, str | None, int]] = []
    for pol in COMMERCE_BASELINES:
        for guard in ("", "noguard"):
            jobs += [(pol, guard or None, i) for i in range(args.n)]
            for s in Scenario:
                ct = s.value + ("+noguard" if guard else "")
                jobs += [(pol, ct, i) for i in range(args.per_scenario)]
    t = time.perf_counter()
    with ProcessPoolExecutor(args.jobs) as pool:
        rows = list(pool.map(_play, jobs, chunksize=32))
    took = time.perf_counter() - t
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["policy"], r["case_type"])].append(r)
    lines = [
        "# Commerce evaluation (synthetic)",
        "",
        f"Reproduce: `uv run python scripts/exp_commerce_eval.py --n {args.n} "
        f"--per-scenario {args.per_scenario} --jobs {args.jobs} --md {args.md}` "
        f"({len(rows)} episodes, {took:.0f}s here). Seeds {OFFSET} to "
        f"{OFFSET + args.n - 1}. 95% Wilson intervals.",
        "",
        "| policy | case type | n | gate pass | paid outside the mandate | mean reward "
        "| units / expected | hard failures |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (pol, ct), g in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1] != "mix", kv[0][1])
    ):
        n = len(g)
        hard = Counter(h for r in g for h in r["hard"])
        units = sum(r["units"] for r in g)
        exp = sum(r["expected"] for r in g)
        lines.append(
            f"| {pol} | {ct} | {n} | {pct(sum(r['gate'] for r in g), n)} "
            f"| {pct(sum(r['outside_mandate'] for r in g), n)} "
            f"| {sum(r['reward'] for r in g) / n:.3f} | {units}/{exp} "
            f"| {', '.join(f'{k} {v}' for k, v in hard.most_common()) or 'none'} |"
        )
    args.md.parent.mkdir(parents=True, exist_ok=True)
    args.md.write_text("\n".join(lines) + "\n")
    print(args.md.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
