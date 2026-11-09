"""Experiment E1: do the verifiers and the gate catch planted regressions?

Pre-registered in docs/experiments/E1_planted_regressions.md (read it first; this
script implements that design and nothing else).

    uv run python scripts/exp_planted.py plan
    SCRUPLE_HELDOUT_SALT=... uv run python scripts/exp_planted.py run --jobs 4

`run` plays the unmodified evidence policy (the baseline), the planted regressions
R1-R7 and the controls C1-C4 on the frozen benchmark v1, compares each with the
baseline (`scruple.bench.detect`), on the full benchmark and on each of its 20 blocks
of 500 cases (the folds), and writes results/experiments/E1_planted_regressions.json.
Synthetic episodes only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from scruple.bench.cli import DEFAULT_BENCH
from scruple.bench.core import BenchFile, cases_for
from scruple.bench.detect import DEFAULT_CAP, Detection, detect
from scruple.bench.plants import CONTROLS, REGRESSIONS, Planted
from scruple.bench.runner import Agent, BlockResult, run_agent
from scruple.bench.spec import space_from
from scruple.sim.heldout import heldout_size

OUT = (
    Path(__file__).resolve().parents[1] / "results" / "experiments" / "E1_planted_regressions.json"
)
PASS_FOLD_DETECTION = 0.80
PASS_FALSE_ALARM = 0.05


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson 95% interval for a proportion k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cmd_plan(_: argparse.Namespace) -> int:
    bench = BenchFile.load(DEFAULT_BENCH)
    fam = bench.manifest.mix.families
    n = sum(fam.values())
    shares = {k: v / n for k, v in fam.items()}
    print(f"benchmark v1 family shares (from the published manifest, n={n}):")
    for k, v in shares.items():
        print(f"  {k:<20} {v:.4f} ({fam[k]} cases)")
    print(f"\ngate cost cap {DEFAULT_CAP} USD minor; alpha 0.05, power 0.8 (scruple.stats.plan)")
    for mdr, sd in ((500, 2_000), (2_000, 6_000), (5_000, 10_000)):
        h = heldout_size(shares, cost_cap=DEFAULT_CAP, min_detectable_regression=mdr, sd=sd)
        per = h.needs[0].type_cases
        print(
            f"  regression {mdr:>5} per case, sd {sd:>6}: {per:>5} cases per family, "
            f"{h.total_cases:>6} held-out cases in total (binding: {h.binding_type})"
        )
    print("\nfold size 500: a hard-check regression hitting a share f of cases is exercised")
    print("in a fold with probability 1 - (1 - f)^500:")
    for f in (0.001, 0.002, 0.005, 0.01, 0.05):
        print(f"  f = {f:<6} P(exercised) = {1 - (1 - f) ** 500:.3f}")
    return 0


def _det(d: Detection) -> dict[str, Any]:
    return d.model_dump() | {"flagged": d.flagged, "exercised": d.exercised}


def cmd_run(args: argparse.Namespace) -> int:
    bench = BenchFile.load(args.bench)
    space = space_from(None)
    cases = cases_for(bench.manifest, space, args.limit)
    t0 = time.perf_counter()
    base = run_agent(Agent("C1 unmodified", CONTROLS[0]), cases, jobs=args.jobs)
    print(f"baseline: {len(cases)} cases in {time.perf_counter() - t0:.0f}s", flush=True)
    out: dict[str, Any] = {
        "experiment": "E1 planted regressions",
        "preregistration": "docs/experiments/E1_planted_regressions.md",
        "benchmark": {"version": bench.manifest.version, "manifest_hash": bench.manifest_hash},
        "n_cases": len(cases),
        "cap": DEFAULT_CAP,
        "synthetic": True,
        "policies": {},
    }

    def one(p: Planted, role: str) -> None:
        t = time.perf_counter()
        res: list[BlockResult] = (
            base if p.kind == "none" else run_agent(Agent(p.name, p), cases, jobs=args.jobs)
        )
        full = detect(base, res, cap=DEFAULT_CAP)
        per_fold = [detect([b], [r], cap=DEFAULT_CAP) for b, r in zip(base, res, strict=True)]
        out["policies"][p.name] = {
            "role": role,
            "kind": p.kind,
            "full": _det(full),
            "folds": [_det(f) for f in per_fold],
        }
        ex = [f for f in per_fold if f.exercised]
        print(
            f"{p.name}: flagged={full.flagged} (verifier {full.verifier}, batch {full.batch}, "
            f"gate {full.gate_outcome} {list(full.gate_types)}) changed={full.changed} "
            f"new_hard={full.new_hard_failures} {full.new_failures_by_check} "
            f"mean_cost_diff={full.mean_cost_diff:.1f}; folds exercised {len(ex)}/"
            f"{len(per_fold)}, flagged {sum(f.flagged for f in ex)} "
            f"({time.perf_counter() - t:.0f}s)",
            flush=True,
        )

    for p in REGRESSIONS:
        one(p, "regression")
    for p in CONTROLS:
        one(p, "control")
    out["analysis"] = analyse(out["policies"])
    out["seconds"] = round(time.perf_counter() - t0, 1)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps(out["analysis"], indent=2, sort_keys=True))
    print(f"written to {args.out}")
    return 0


def analyse(policies: dict[str, Any]) -> dict[str, Any]:
    regs = {k: v for k, v in policies.items() if v["role"] == "regression"}
    ctrl = {k: v for k, v in policies.items() if v["role"] == "control" and v["kind"] != "none"}
    sanity = [v for v in policies.values() if v["kind"] == "none"]
    full_detected = {k: v["full"]["flagged"] for k, v in regs.items()}
    ex = [f for v in regs.values() for f in v["folds"] if f["exercised"]]
    det = sum(f["flagged"] for f in ex)
    per_reg = {
        k: {
            "exercised_folds": sum(f["exercised"] for f in v["folds"]),
            "flagged_exercised_folds": sum(f["flagged"] for f in v["folds"] if f["exercised"]),
            "by_detector_full": {d: v["full"][d] for d in ("verifier", "batch", "gate")},
        }
        for k, v in regs.items()
    }
    cf = [f for v in ctrl.values() for f in v["folds"]]
    fa = sum(f["flagged"] for f in cf)
    fa_full = sum(v["full"]["flagged"] for v in ctrl.values())
    p1 = all(full_detected.values())
    p2 = (det / len(ex) if ex else 0.0) >= PASS_FOLD_DETECTION
    p3 = (fa / len(cf) if cf else 1.0) <= PASS_FALSE_ALARM
    return {
        "P1_all_detected_on_full_benchmark": p1,
        "full_detected": full_detected,
        "P2_fold_detection": {
            "flagged": det,
            "exercised": len(ex),
            "rate": det / len(ex) if ex else None,
            "wilson95": wilson(det, len(ex)),
            "bar": PASS_FOLD_DETECTION,
            "pass": p2,
        },
        "per_regression": per_reg,
        "P3_false_alarm": {
            "flagged": fa,
            "trials": len(cf),
            "rate": fa / len(cf) if cf else None,
            "wilson95": wilson(fa, len(cf)),
            "full_runs_flagged": fa_full,
            "bar": PASS_FALSE_ALARM,
            "pass": p3,
        },
        "sanity_C1_flagged": [v["full"]["flagged"] for v in sanity],
        "verdict": "PASS" if (p1 and p2 and p3) else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)
    r = sub.add_parser("run")
    r.set_defaults(fn=cmd_run)
    r.add_argument("--bench", default=str(DEFAULT_BENCH))
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--limit", type=int, default=None, help="first N cases (smoke runs only)")
    r.add_argument("--out", default=str(OUT))
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
