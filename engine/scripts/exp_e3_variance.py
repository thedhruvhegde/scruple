"""Experiment E3: does the variance-reduced gate catch E1's cost-only regressions?

Pre-registered in docs/experiments/E3_variance_reduction.md (read it first; this
script implements that design and nothing else). Synthetic episodes only.

    uv run python scripts/exp_e3_variance.py plan
    SCRUPLE_HELDOUT_SALT=... uv run python scripts/exp_e3_variance.py run --jobs 4
    uv run python scripts/exp_e3_variance.py analyse   # from the saved per-case data

`run` replays E1's policies (scruple.bench.plants: baseline C1, regressions R1-R7,
controls C2-C4) on the frozen benchmark v1 exactly as scripts/exp_planted.py does,
plus the baseline with independent issuer draws (to measure common random numbers).
It saves one row per case (covariates and each policy's cost) to
results/experiments/E3_variance_reduction/cases.json, then `analyse` compares E1's
gate (scruple.bench.detect, unchanged) with the variance-reduced gate
(scruple.stats.evaluate_pairs_vr) on the full benchmark and on the 20 folds, and
writes results.json. E1's pre-registered results are not touched.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import scruple.agents.env as agent_env
from scruple.bench.cli import DEFAULT_BENCH
from scruple.bench.core import BenchFile, cases_for
from scruple.bench.detect import DEFAULT_CAP, detect
from scruple.bench.plants import CONTROLS, REGRESSIONS, Planted
from scruple.bench.runner import Agent, BlockResult, run_agent
from scruple.bench.spec import space_from
from scruple.sim.issuer import IssuerParams
from scruple.stats import LossPair, StatsGateConfig, VRGateConfig, crn_report, evaluate_pairs_vr
from scruple.stats.planning import binary_kl

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "results" / "experiments" / "E3_variance_reduction"
E1_JSON = ROOT / "results" / "experiments" / "E1_planted_regressions.json"
ALPHA = 0.05
SHARE = 0.8  # VRGateConfig default, fixed before the run
BAND_EDGES = (8_000, 1_000_000)  # GateConfig.evalue_band_edges_usd_minor defaults
FOLD = 500
PASS_FALSE_ALARM = 0.05
ISSUER_SHIFT = 0x9E3779B1  # independent issuer draws for the CRN measurement


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def band(amount: int) -> str:
    for e in BAND_EDGES:
        if amount < e:
            return f"<{e}"
    return f">={BAND_EDGES[-1]}"


# --- plan (before the run) ------------------------------------------------------------


def cmd_plan(_: argparse.Namespace) -> int:
    e1 = json.loads(E1_JSON.read_text())
    n = e1["n_cases"]
    cap = e1["cap"]
    print(f"E1 (published): n={n}, cap={cap} USD minor, gate alpha {ALPHA}")
    print("Proposition 9.7 budget B = n mu+ / cap (pooled, no improving subgroup):")
    for name, v in e1["policies"].items():
        if v["role"] != "regression":
            continue
        mu = v["full"]["mean_cost_diff"]
        print(f"  {name[:44]:<44} mean {mu:>7.1f} minor/case -> B = {n * max(mu, 0) / cap:.3f}")
    for a in (ALPHA, SHARE * ALPHA):
        print(
            f"kl(1-beta || {a:g}) needed: power 0.5 -> {binary_kl(0.5, a):.3f}, "
            f"0.8 -> {binary_kl(0.8, a):.3f}, log(1/alpha) = {math.log(1 / a):.3f}"
        )
    return 0


# --- run (needs the salt) ---------------------------------------------------------------


def _shifted_issuer(**kw: Any) -> IssuerParams:
    kw["seed"] = int(kw.get("seed", 0)) ^ ISSUER_SHIFT
    return IssuerParams(**kw)


def _rows(res: Sequence[BlockResult]) -> list[dict[str, Any]]:
    return [
        {
            "i": r.index,
            "family": r.family,
            "tier": r.tier,
            "amount": r.amount_minor,
            "cost": r.cost,
            "changed_key": [r.cost, list(r.failed), r.tool_calls, r.won, r.reward],
        }
        for b in res
        for r in b.rows
    ]


def _played(
    key: str, play: Callable[[], list[BlockResult]], cache: Path | None
) -> list[BlockResult]:
    """Play once; with ``--cache`` (outside the repository) a crash loses one policy only."""
    f = None if cache is None else cache / f"{key}.json"
    if f is not None and f.exists():
        return [BlockResult.model_validate(b) for b in json.loads(f.read_text())]
    t = time.perf_counter()
    res = play()
    print(f"{key}: {time.perf_counter() - t:.0f}s", flush=True)
    if f is not None:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps([b.model_dump(mode="json") for b in res]))
    return res


def _alt_baseline(cases: Sequence[Any], jobs: int) -> list[BlockResult]:
    original = agent_env.IssuerParams
    setattr(agent_env, "IssuerParams", _shifted_issuer)  # noqa: B010 - measurement only
    try:
        return run_agent(Agent("C1 independent issuer draws", CONTROLS[0]), cases, jobs=jobs)
    finally:
        setattr(agent_env, "IssuerParams", original)  # noqa: B010


def cmd_run(args: argparse.Namespace) -> int:
    bench = BenchFile.load(args.bench)
    cases = cases_for(bench.manifest, space_from(None), args.limit)
    cache = Path(args.cache) if args.cache else None
    t0 = time.perf_counter()
    runs: dict[str, list[BlockResult]] = {}
    base = _played(
        "C1", lambda: run_agent(Agent("C1 unmodified", CONTROLS[0]), cases, jobs=args.jobs), cache
    )
    runs[CONTROLS[0].name] = base
    policies: list[tuple[Planted, str]] = [(p, "regression") for p in REGRESSIONS] + [
        (p, "control") for p in CONTROLS[1:]
    ]
    for p, _role in policies:
        runs[p.name] = _played(
            p.name[:2], lambda p=p: run_agent(Agent(p.name, p), cases, jobs=args.jobs), cache
        )
    alt = _played("C1_alt_issuer", lambda: _alt_baseline(cases, args.jobs), cache)
    base_rows = _rows(base)
    data: dict[str, Any] = {
        "experiment": "E3 variance reduction",
        "preregistration": "docs/experiments/E3_variance_reduction.md",
        "benchmark": {"version": bench.manifest.version, "manifest_hash": bench.manifest_hash},
        "synthetic": True,
        "n_cases": len(cases),
        "cases": [{k: r[k] for k in ("i", "family", "tier", "amount")} for r in base_rows],
        "baseline_cost": [r["cost"] for r in base_rows],
        "baseline_alt_issuer_cost": [r["cost"] for r in _rows(alt)],
        "policies": {},
        "seconds": 0.0,
    }
    roles = {CONTROLS[0].name: ("control", "none")} | {p.name: (r, p.kind) for p, r in policies}
    for name, res in runs.items():
        rows = _rows(res)
        full = detect(base, res, cap=DEFAULT_CAP)  # E1's detector, unchanged
        folds = [detect([b], [r], cap=DEFAULT_CAP) for b, r in zip(base, res, strict=True)]
        data["policies"][name] = {
            "role": roles[name][0],
            "kind": roles[name][1],
            "cost": [r["cost"] for r in rows],
            "changed": [
                r["changed_key"] != b["changed_key"] for r, b in zip(rows, base_rows, strict=True)
            ],
            "e1_detector_full": full.model_dump() | {"flagged": full.flagged},
            "e1_detector_folds": [f.model_dump() | {"flagged": f.flagged} for f in folds],
        }
    data["seconds"] = round(time.perf_counter() - t0, 1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "cases.json").write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"written {OUT_DIR / 'cases.json'} ({data['seconds']}s)")
    return cmd_analyse(args)


# --- analysis (from saved data only) ----------------------------------------------------


def _pairs(
    idx: Sequence[int], base: Sequence[int], cand: Sequence[int], typ: Callable[[int], str]
) -> list[LossPair]:
    out = []
    for i in idx:
        d = max(-DEFAULT_CAP, min(DEFAULT_CAP, cand[i] - base[i]))  # E1's clipping
        out.append(
            LossPair(
                case_id=str(i),
                case_type=typ(i),
                baseline_loss=float(base[i]),
                candidate_loss=float(base[i] + d),
            )
        )
    return out


def _vr(pairs: list[LossPair], max_bet: float = 0.5) -> dict[str, Any]:
    cfg = VRGateConfig(
        base=StatsGateConfig(cost_cap=float(DEFAULT_CAP), alpha_block=ALPHA, shuffle_seed=0),
        stratified_share=SHARE,
        max_bet=max_bet,
    )
    v = evaluate_pairs_vr(pairs, cfg)
    return {
        "outcome": v.outcome,
        "blocked_by": list(v.blocked_by),
        "stratified_e_value": v.stratified_e_value,
        "stratified_threshold": v.stratified_threshold,
        "family_types": list(v.base.regressed_fwer),
        "contributions": v.contributions,
    }


def _budget(pairs: Sequence[LossPair]) -> dict[str, float]:
    by: dict[str, list[float]] = defaultdict(list)
    for p in pairs:
        by[p.case_type].append(p.diff)
    n = len(pairs)
    pooled = math.fsum(p.diff for p in pairs) / n
    strat = math.fsum(max(math.fsum(v) / len(v), 0.0) * len(v) for v in by.values())
    return {"pooled": n * max(pooled, 0.0) / DEFAULT_CAP, "stratified": strat / DEFAULT_CAP}


def cmd_analyse(_: argparse.Namespace) -> int:
    data = json.loads((OUT_DIR / "cases.json").read_text())
    cases = data["cases"]
    n = data["n_cases"]
    base = data["baseline_cost"]
    alt = data["baseline_alt_issuer_cost"]

    def fam(i: int) -> str:
        return str(cases[i]["family"])

    def fam_band(i: int) -> str:
        return f"{cases[i]['family']}/{band(int(cases[i]['amount']))}"

    allidx = list(range(n))
    folds = [list(range(s, min(s + FOLD, n))) for s in range(0, n, FOLD)]
    out: dict[str, Any] = {
        "experiment": data["experiment"],
        "preregistration": data["preregistration"],
        "benchmark": data["benchmark"],
        "synthetic": True,
        "n_cases": n,
        "cap": DEFAULT_CAP,
        "alpha": ALPHA,
        "stratified_share": SHARE,
        "policies": {},
    }
    for name, v in data["policies"].items():
        cost = v["cost"]
        full_pairs = _pairs(allidx, base, cost, fam)
        diffs = [c - b for b, c in zip(base, cost, strict=True)]
        fold_vr = [_vr(_pairs(f, base, cost, fam)) for f in folds]
        exercised = [any(v["changed"][i] for i in f) for f in folds]
        rec: dict[str, Any] = {
            "role": v["role"],
            "kind": v["kind"],
            "mean_cost_diff_minor": math.fsum(diffs) / n,
            "changed": sum(v["changed"]),
            "clipped": sum(abs(d) > DEFAULT_CAP for d in diffs),
            "e1_gate_full": v["e1_detector_full"]["gate_outcome"],
            "e1_gate_types": v["e1_detector_full"]["gate_types"],
            "e1_gate_folds_blocked": sum(f["gate"] for f in v["e1_detector_folds"]),
            "vr_full": _vr(full_pairs),
            "vr_full_family_band": _vr(_pairs(allidx, base, cost, fam_band)),
            "vr_folds_blocked": sum(f["outcome"] == "block" for f in fold_vr),
            "vr_folds_blocked_exercised": sum(
                f["outcome"] == "block" for f, e in zip(fold_vr, exercised, strict=True) if e
            ),
            "folds_exercised": sum(exercised),
            "budget_family": _budget(full_pairs),
            "budget_family_band": _budget(_pairs(allidx, base, cost, fam_band)),
        }
        if v["kind"] != "none":
            same = crn_report([float(b) for b in base], [float(c) for c in cost])
            other = crn_report([float(b) for b in alt], [float(c) for c in cost])
            rec["crn"] = {
                "var_paired_same_draws": same.var_paired,
                "var_paired_independent_draws": other.var_paired,
                "var_unpaired": same.var_unpaired,
                "share_identical_same_draws": same.share_identical,
                "share_identical_independent_draws": other.share_identical,
                "corr_same_draws": same.correlation,
            }
        out["policies"][name] = rec
    regs = {k: v for k, v in out["policies"].items() if v["role"] == "regression"}
    ctrl = {k: v for k, v in out["policies"].items() if v["role"] == "control"}
    r5 = next(v for k, v in regs.items() if k.startswith("R5"))
    ctrl_fold_trials = sum(len(folds) for v in ctrl.values() if v["kind"] != "none")
    ctrl_fold_blocks = sum(v["vr_folds_blocked"] for v in ctrl.values() if v["kind"] != "none")
    p1 = r5["vr_full"]["outcome"] == "block"
    p2 = not any(v["vr_full"]["outcome"] == "block" for v in ctrl.values())
    rate = ctrl_fold_blocks / ctrl_fold_trials if ctrl_fold_trials else 1.0
    p3 = rate <= PASS_FALSE_ALARM
    out["analysis"] = {
        "P1_R5_blocked_full": p1,
        "P2_no_control_blocked_full": p2,
        "P3_control_fold_false_blocks": {
            "blocked": ctrl_fold_blocks,
            "trials": ctrl_fold_trials,
            "rate": rate,
            "wilson95": wilson(ctrl_fold_blocks, ctrl_fold_trials),
            "bar": PASS_FALSE_ALARM,
            "pass": p3,
        },
        "verdict": "PASS" if (p1 and p2 and p3) else "FAIL",
    }
    (OUT_DIR / "results.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    _print(out)
    return 0


def cmd_posthoc(_: argparse.Namespace) -> int:
    """NOT pre-registered (added after the run): the stratified process with a bet cap
    of 0.95 instead of 0.5, on the full benchmark and the folds, every policy."""
    data = json.loads((OUT_DIR / "cases.json").read_text())
    cases, n, base = data["cases"], data["n_cases"], data["baseline_cost"]
    allidx = list(range(n))
    folds = [list(range(s, min(s + FOLD, n))) for s in range(0, n, FOLD)]

    def fam(i: int) -> str:
        return str(cases[i]["family"])

    out: dict[str, Any] = {"note": "post hoc, not pre-registered; max_bet 0.95", "policies": {}}
    for name, v in data["policies"].items():
        full = _vr(_pairs(allidx, base, v["cost"], fam), max_bet=0.95)
        fb = sum(_vr(_pairs(f, base, v["cost"], fam), 0.95)["outcome"] == "block" for f in folds)
        out["policies"][name] = {"full": full, "folds_blocked": fb}
        print(
            f"{name}: {full['outcome']} {full['blocked_by']} stratified e "
            f"{full['stratified_e_value']:.3g} (vs {full['stratified_threshold']:.3g}); "
            f"folds blocked {fb}/{len(folds)}"
        )
    (OUT_DIR / "posthoc_wide_bet.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _resampled_block(xs: list[tuple[str, float]], wide: bool) -> tuple[bool, bool]:
    """(E1 block family, VR rule) on one stream of (family, X): default or 0.95 bets."""
    from scruple.stats import BettingEProcess, StratifiedBettingEProcess

    mb = 0.95 if wide else 0.5
    fams = sorted({k for k, _ in xs})
    per = {k: BettingEProcess(0.0, "greater", max_bet=mb) for k in fams}
    pooled = BettingEProcess(0.0, "greater", max_bet=mb)
    strat = StratifiedBettingEProcess("greater", max_bet=mb)
    for k, x in xs:
        per[k].update(x)
        pooled.update(x)
        strat.update(k, x)
    top = max(max(p.max_log_wealth for p in per.values()), pooled.max_log_wealth)
    k1 = len(fams) + 1
    e1 = top >= math.log(k1 / ALPHA)
    vr = top >= math.log(k1 / ((1 - SHARE) * ALPHA)) or strat.max_log_wealth >= math.log(
        1 / (SHARE * ALPHA)
    )
    return e1, vr


def cmd_resample(args: argparse.Namespace) -> int:
    """NOT pre-registered: P(block) when 10,000 cases are resampled i.i.d. from a
    policy's paired costs (seeded), E1's family and the VR rule, default and wide bets."""
    import random

    data = json.loads((OUT_DIR / "cases.json").read_text())
    cases, n, base = data["cases"], data["n_cases"], data["baseline_cost"]
    out: dict[str, Any] = {"note": "post hoc, not pre-registered", "reps": args.reps}
    for name in args.policies:
        v = next(p for k, p in data["policies"].items() if k.startswith(name))
        pool = [
            (str(cases[i]["family"]), max(-1.0, min(1.0, (v["cost"][i] - base[i]) / DEFAULT_CAP)))
            for i in range(n)
        ]
        rng = random.Random(args.seed)
        hits = {"e1_default": 0, "vr_default": 0, "e1_wide": 0, "vr_wide": 0}
        for _ in range(args.reps):
            xs = [pool[rng.randrange(n)] for _ in range(n)]
            for wide in (False, True):
                e1, vr = _resampled_block(xs, wide)
                tag = "wide" if wide else "default"
                hits[f"e1_{tag}"] += e1
                hits[f"vr_{tag}"] += vr
        out[name] = {k: h / args.reps for k, h in hits.items()}
        print(name, out[name], flush=True)
    (OUT_DIR / "posthoc_resampled.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )
    return 0


def _print(out: dict[str, Any]) -> None:
    print(f"E3 on benchmark v1, n={out['n_cases']}, cap {out['cap']}, alpha {out['alpha']}")
    for name, v in out["policies"].items():
        vf, vb = v["vr_full"], v["vr_full_family_band"]
        print(
            f"{name}: mean {v['mean_cost_diff_minor']:.1f} minor/case, changed {v['changed']}, "
            f"clipped {v['clipped']}; E1 gate {v['e1_gate_full']} {v['e1_gate_types']}, "
            f"folds {v['e1_gate_folds_blocked']}/20; VR {vf['outcome']} {vf['blocked_by']} "
            f"(stratified e {vf['stratified_e_value']:.3g} vs {vf['stratified_threshold']:.3g}), "
            f"folds {v['vr_folds_blocked']}/20 (exercised {v['vr_folds_blocked_exercised']}/"
            f"{v['folds_exercised']}); family/band VR {vb['outcome']} "
            f"(e {vb['stratified_e_value']:.3g}); budget family "
            f"{v['budget_family']['stratified']:.3f} (pooled {v['budget_family']['pooled']:.3f}), "
            f"family/band {v['budget_family_band']['stratified']:.3f}"
        )
        if "crn" in v:
            c = v["crn"]
            print(
                f"    CRN: var paired (same draws) {c['var_paired_same_draws']:.4g}, "
                f"(independent draws) {c['var_paired_independent_draws']:.4g}, unpaired "
                f"{c['var_unpaired']:.4g}; identical {c['share_identical_same_draws']:.3f} vs "
                f"{c['share_identical_independent_draws']:.3f}"
            )
    print(json.dumps(out["analysis"], indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)
    sub.add_parser("analyse").set_defaults(fn=cmd_analyse)
    sub.add_parser("posthoc").set_defaults(fn=cmd_posthoc)
    rs = sub.add_parser("resample")
    rs.set_defaults(fn=cmd_resample)
    rs.add_argument("--reps", type=int, default=100)
    rs.add_argument("--seed", type=int, default=0)
    rs.add_argument("--policies", nargs="*", default=["R5", "R1", "R4", "C3"])
    r = sub.add_parser("run")
    r.set_defaults(fn=cmd_run)
    r.add_argument("--bench", default=str(DEFAULT_BENCH))
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--limit", type=int, default=None, help="first N cases (smoke runs only)")
    r.add_argument("--cache", default=None, help="directory outside the repo for per-policy runs")
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
