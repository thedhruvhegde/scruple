"""Scale check for `scruple suite`: a 5,000-scenario bank, built twice (serial and with
worker processes) to show the hashes do not depend on the worker count, verified with
the salt, played end to end by two local agents, and reported.

    uv run python results/suite_scale/bench.py [--size 5000] [--workers 3]

The salt in results/suite_scale/demo.salt is public on purpose (a demo bank); a client
bank's salt is secret. Writes results/suite_scale/scale.json and SCALE.md. Timings are
for this machine (CPU count recorded) and vary run to run; the hashes do not.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from scruple.suite.agents import PythonAgent, ScriptedAgent
from scruple.suite.envs import ENV_NAMES
from scruple.suite.plan import build_plan, verify_plan, write_plan
from scruple.suite.report import build_report, write_report
from scruple.suite.run import run_suite

HERE = Path(__file__).parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    salt = (HERE / "demo.salt").read_text().strip()
    out: dict = {"size": args.size, "seed": args.seed, "cpus": os.cpu_count(), "python": platform.python_version()}
    t = time.perf_counter()
    p1, _ = build_plan(ENV_NAMES, args.size, seed=args.seed, salt=salt, workers=1)
    out["plan_serial_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    p2, _ = build_plan(ENV_NAMES, args.size, seed=args.seed, salt=salt, workers=args.workers)
    out["plan_parallel_s"] = round(time.perf_counter() - t, 2)
    out["workers"] = args.workers
    assert p1["plan_hash"] == p2["plan_hash"], "worker count changed the bank"
    out["hashes"] = {k: p1[k] for k in ("plan_hash", "bank_hash", "commitments_hash", "salt_commitment")}
    out["blocks"] = p1["blocks"]
    sc = p1["scenarios"]
    out["mix"] = {
        "env": dict(Counter(s["env"] for s in sc)),
        "band": dict(Counter(s["band"] for s in sc)),
        "adversarial": sum(bool(s["adversarial"]) for s in sc),
        "families": len({(s["env"], s["family"]) for s in sc}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        plan = d / "plan.json"
        write_plan(p1, salt, plan)
        out["plan_bytes"] = plan.stat().st_size
        t = time.perf_counter()
        probs = verify_plan(p1, salt=salt)
        out["verify_with_salt_s"] = round(time.perf_counter() - t, 2)
        assert probs == [], probs[:3]
        runs = {}
        for name, agent in (
            ("scripted-honest", ScriptedAgent("honest")),
            ("python-read_then_submit", PythonAgent("scruple.suite.example_agents:read_then_submit")),
        ):
            t = time.perf_counter()
            run_suite(plan, agent, d / name, concurrency=args.workers)
            dt = time.perf_counter() - t
            runs[name] = {"wall_s": round(dt, 2), "scenarios_per_s": round(args.size / dt, 1)}
        t = time.perf_counter()
        rep = build_report(d / "python-read_then_submit", baseline=d / "scripted-honest", salt=salt, verify_limit=None)
        files = write_report(rep, d / "report", ["json", "html", "pdf"])
        runs["report_s"] = round(time.perf_counter() - t, 2)
        runs["report_bytes"] = {f.name: f.stat().st_size for f in files}
        runs["naive_vs_honest"] = {
            "verdict": rep["executive"]["verdict"],
            "gate_rate": rep["executive"]["overall"]["gate_rate"],
            "reward_diff": rep["regression"]["reward_diff"],
            "reward_diff_ci": rep["regression"]["reward_diff_ci"],
            "regenerated_and_revealed": rep["integrity"]["cases_regenerated"],
            "problems": rep["integrity"]["regeneration_problems"],
        }
        shutil.copy(d / "report" / "report.json", d / "keep.json")
        out["runs"] = runs
    (HERE / "scale.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    r = out["runs"]
    md = [
        "# Suite scale check",
        "",
        f"`uv run python results/suite_scale/bench.py` on {out['cpus']} CPUs, Python {out['python']}. "
        "Synthetic scenarios; public demo salt.",
        "",
        "| step | result |",
        "|---|---|",
        f"| plan {args.size} scenarios, serial | {out['plan_serial_s']} s ({args.size / out['plan_serial_s']:.0f}/s) |",
        f"| plan {args.size} scenarios, {args.workers} workers | {out['plan_parallel_s']} s, identical plan hash |",
        f"| verify (regenerate every case, reveal every commitment) | {out['verify_with_salt_s']} s, 0 problems |",
        f"| run scripted honest reference | {r['scripted-honest']['wall_s']} s ({r['scripted-honest']['scenarios_per_s']}/s) |",
        f"| run naive python agent | {r['python-read_then_submit']['wall_s']} s ({r['python-read_then_submit']['scenarios_per_s']}/s) |",
        f"| report (json, html, pdf) with baseline, all {args.size} cases regenerated | {r['report_s']} s |",
        "",
        f"plan_hash `{out['hashes']['plan_hash']}`  ",
        f"bank_hash `{out['hashes']['bank_hash']}`  ",
        f"commitments_hash `{out['hashes']['commitments_hash']}`  ",
        f"{len(out['blocks'])} block hashes of 500 in scale.json. Mix: {out['mix']}.",
        "",
        f"Naive agent against the honest reference: {r['naive_vs_honest']}.",
    ]
    (HERE / "SCALE.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
