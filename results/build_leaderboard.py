"""Build the leaderboard on results/index.html from the engine's real result files.

Run from the engine directory:

    cd engine && uv run python ../results/build_leaderboard.py

It reads, and never changes:
  results/real/models/summary.json                     CFPB tasks, Claude (subagent) vs baselines
  results/real/open_models/cfpb/regime_completed/       CFPB regime, the 22 items every model answered
  results/real/open_models/cfpb/regime-2015.*.jsonl     how many regime items each open model answered
  results/real/episodes/episodes.json                  Claude episodes as first published
  results/real/open_models/episodes/*/runs/seed-*.json  open-model episodes, one scored row each
  results/real/open_models/e5_episodes.json            all systems on seeds 5001-5012, current verifiers
  results/real/incentives/e4.json                      Claude, honest and incentive briefs, seeds 6001-6012

Every number is copied or counted from those files. The only numbers computed here are
Wilson 95% intervals on counts (k of n) and the checks below. The script stops if a
count recomputed from the per-episode rows differs from the published summary.
Output: results/data/leaderboard.json. Cases are synthetic; the model behaviour is real.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

ENGINE = Path.cwd()
REAL = ENGINE / "results" / "real"
OUT = ENGINE.parent / "results" / "data" / "leaderboard.json"
CLAUDE = ("haiku", "sonnet", "opus")
OPEN = ("gpt-oss-120b", "gpt-oss-20b", "qwen3.8-27b")
REFS = ("ref:evidence", "ref:oracle", "ref:always_accept")

NAME = {
    "haiku": "Claude Haiku",
    "sonnet": "Claude Sonnet",
    "opus": "Claude Opus",
    "gpt-oss-120b": "gpt-oss-120b",
    "gpt-oss-20b": "gpt-oss-20b",
    "qwen3.8-27b": "Qwen3.8 27B",
    "ref:evidence": "Evidence policy",
    "ref:oracle": "Oracle (reads the truth)",
    "ref:always_accept": "Always accept",
    "tfidf_lr": "TF-IDF + LR",
    "keywords": "Keyword rules",
    "majority": "Majority class",
}
CFPB_KEY = {"haiku": "claude-haiku", "sonnet": "claude-sonnet"}  # names in regime_completed


def load(p: Path) -> Any:
    return json.loads(p.read_text())


def fail(msg: str) -> None:
    sys.exit(f"build_leaderboard: {msg}")


def wilson(k: int, n: int) -> list[float]:
    """Wilson score interval, 95%, rounded to 3 places."""
    if n == 0:
        return [0.0, 1.0]
    z = 1.959964
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def count(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "ci95": wilson(k, n)}


def cfpb_cell(entry: dict[str, Any], metric: str, n: int) -> dict[str, Any]:
    cell: dict[str, Any] = {"value": round(entry[metric], 3), "metric": metric, "n": n}
    if "valid_format" in entry:
        cell["valid"] = entry["valid_format"]
    vs = entry.get("vs_tfidf_lr")
    if vs:
        cell["vs_tfidf"] = {"diff": round(vs["diff"], 3), "ci95": [round(x, 3) for x in vs["ci95"]]}
    return cell


def main() -> None:
    if not REAL.is_dir():
        fail("run from the engine directory (cd engine)")

    summary = load(REAL / "models" / "summary.json")
    shared = load(REAL / "open_models" / "cfpb" / "regime_completed" / "summary.json")["regime-2015"]
    published = load(REAL / "episodes" / "episodes.json")
    e5 = load(REAL / "open_models" / "e5_episodes.json")
    e4 = load(REAL / "incentives" / "e4.json")
    e5_rows = {r["system"]: r for r in e5["systems"]}
    checks: list[str] = []

    # ---------- per-episode rows, recounted ----------
    by_sys: dict[str, list[dict[str, Any]]] = {}
    for r in published:
        by_sys.setdefault(r["system"], []).append(r)
    for m in OPEN:
        runs = sorted((REAL / "open_models" / "episodes" / m / "runs").glob("seed-*.json"))
        by_sys[m] = [load(p)["row"] for p in runs]

    def recount(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(rows),
            "gate_pass": sum(bool(r["gate"]) for r in rows),
            "fair_outcome": sum(bool(r["fair_outcome"]) for r in rows),
            "sr01_fail": sum("SR-01" in r["hard_failures"] for r in rows),
            "mean_cost_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 2),
        }

    ref_cost = {r["seed"]: r["cost_usd"] for r in by_sys["ref:evidence"]}
    sr01_notes: dict[str, str] = {}
    for s in CLAUDE + OPEN + REFS:
        got, want = recount(by_sys[s]), e5_rows[s]
        keys = ("n", "gate_pass", "fair_outcome", "mean_cost_usd") + (() if s in CLAUDE else ("sr01_fail",))
        for k in keys:
            if abs(got[k] - want[k]) > 0.006:
                fail(f"{s} {k}: recounted {got[k]}, e5_episodes.json says {want[k]}")
        if s in CLAUDE and got["sr01_fail"] != want["sr01_fail"]:
            # the Claude rows in e5 are the same call logs re-scored with the current verifiers
            sr01_notes[s] = (
                f"SR-01 {want['sr01_fail']}/{want['n']} as re-scored with the current verifiers; "
                f"{got['sr01_fail']}/{got['n']} as first published in episodes.json. The structured "
                "claims parser (VERIFIER_FINDINGS G-11) dropped one false positive; the gate is unchanged."
            )
        cv = want["cost_vs_evidence"]
        if cv is not None:
            seeds = [r["seed"] for r in by_sys[s]]
            diff = sum(r["cost_usd"] - ref_cost[r["seed"]] for r in by_sys[s]) / len(seeds)
            if abs(diff - cv[0]) > 0.006:
                fail(f"{s} cost vs evidence: recounted {diff:.2f}, e5 says {cv[0]}")
    checks.append("episode counts and mean costs recounted from per-episode rows match e5_episodes.json")
    checks.append("paired cost differences against the evidence policy recomputed per seed match e5_episodes.json")

    # ---------- how many regime items the open models answered ----------
    answered: dict[str, int] = {}
    for m in ("gpt-oss-120b", "gpt-oss-20b"):
        p = REAL / "open_models" / "cfpb" / f"regime-2015.{m}.answers.jsonl"
        answered[m] = sum(1 for line in p.read_text().splitlines() if line.strip())
    n_full = summary["regime-2015"]["n"]

    # ---------- rows ----------
    rows: list[dict[str, Any]] = []

    def episodes(s: str) -> dict[str, Any]:
        w = e5_rows[s]
        cv = w["cost_vs_evidence"]
        return {
            "seeds": f"{min(r['seed'] for r in by_sys[s])}-{max(r['seed'] for r in by_sys[s])}",
            "n": w["n"],
            "gate_pass": count(w["gate_pass"], w["n"]),
            "fair_outcome": count(w["fair_outcome"], w["n"]),
            "sr01_fail": count(w["sr01_fail"], w["n"]),
            "mean_cost_usd": w["mean_cost_usd"],
            "cost_vs_evidence": None if cv is None else {"diff": cv[0], "ci95": [cv[1], cv[2]]},
            "hard_failures": w["hard_failures"],
        }

    def e4_arms(s: str) -> dict[str, Any]:
        a = e4["arms"][s]
        return {
            arm: {
                "n": a[arm]["n"],
                "gate_pass": count(a[arm]["gate_pass"], a[arm]["n"]),
                "sr01_fail": count(a[arm]["self_report_fail"], a[arm]["n"]),
                "mean_cost_usd": a[arm]["mean_cost_usd"],
            }
            for arm in ("honest", "incentive")
        }

    for s in CLAUDE:
        cfpb: dict[str, Any] = {}
        if s in summary["regime-2015"]["entries"]:
            for task, metric in (("regime-2015", "accuracy"), ("outcome-2015", "auroc"), ("routing-2025", "accuracy")):
                cfpb[task] = cfpb_cell(summary[task]["entries"][s], metric, summary[task]["n"])
            cfpb["regime-shared"] = cfpb_cell(shared["entries"][CFPB_KEY[s]], "accuracy", shared["n"])
        partial = [] if cfpb else ["CFPB tasks not run for Opus"]
        rows.append({
            "id": s, "name": NAME[s], "model": f"Claude {s.capitalize()}, November 2026 release",
            "harness": "subagent", "harness_label": "Claude Code subagent, scruple episode CLI",
            "episodes": episodes(s), "cfpb": cfpb, "e4": e4_arms(s),
            "partial": partial, "notes": [sr01_notes[s]] if s in sr01_notes else [],
        })

    for s in OPEN:
        meta = e5["open_runs"][s]
        cfpb = {}
        partial: list[str] = []
        if s in shared["entries"]:
            cfpb["regime-shared"] = cfpb_cell(shared["entries"][s], "accuracy", shared["n"])
            partial.append(f"CFPB regime: answered {answered[s]} of {n_full} items before the daily token cap; scored on the first 22, which both gpt-oss models answered")
            partial.append("CFPB relief and routing not run")
        else:
            partial.append("CFPB tasks not run")
        stops = meta["stops"]
        if e5_rows[s]["n"] < len(e5["seeds"]):
            lapsed = {k: v for k, v in stops.items() if k != "submitted"}
            partial.append(
                f"episodes: {e5_rows[s]['n']} of {len(e5['seeds'])} seeds before the daily token cap"
                + (f"; {sum(lapsed.values())} lapsed on an API error and is scored as played" if lapsed else "")
            )
        rows.append({
            "id": s, "name": NAME[s], "model": f"{meta['model']} via the Groq API",
            "harness": "api", "harness_label": "raw API, scruple eval run, zero-shot",
            "episodes": episodes(s), "cfpb": cfpb, "e4": None, "partial": partial,
            "notes": [f"{meta['rejected_turns']} generations refused by the API (tool-call format), counted as calls to unknown tools"] if meta["rejected_turns"] else [],
            "tokens": {"input": meta["input_tokens"], "output": meta["output_tokens"], "turns": meta["turns"]},
        })

    for s in REFS:
        rows.append({
            "id": s, "name": NAME[s], "model": "reference policy, written by hand",
            "harness": "reference", "harness_label": "in-process reference policy",
            "episodes": episodes(s), "cfpb": {}, "e4": None, "partial": [], "notes": [],
        })

    for b in ("tfidf_lr", "keywords", "majority"):
        cfpb = {
            task: cfpb_cell(summary[task]["entries"][b], metric, summary[task]["n"])
            for task, metric in (("regime-2015", "accuracy"), ("outcome-2015", "auroc"), ("routing-2025", "accuracy"))
        }
        cfpb["regime-shared"] = cfpb_cell(shared["entries"][b], "accuracy", shared["n"])
        rows.append({
            "id": b, "name": NAME[b], "model": "baseline fitted on train, chosen on dev",
            "harness": "baseline", "harness_label": "CFPB baseline, no episodes",
            "episodes": None, "cfpb": cfpb, "e4": None, "partial": [], "notes": [],
        })

    out = {
        "note": "Real CFPB complaints and real model behaviour on synthetic dispute cases. Built by results/build_leaderboard.py.",
        "episodes": {
            "seeds": f"{e5['seeds'][0]}-{e5['seeds'][-1]}",
            "reference": "ref:evidence",
            "intervals": "cost: paired bootstrap over seeds, 2,000 resamples (from e5_episodes.json); counts: Wilson 95%",
        },
        "e4": {"seeds": "6001-6012", "h1": e4["H1"], "h3": e4["H3"], "unplayed_excluded": e4["unplayed_excluded"]},
        "cfpb": {
            "regime-2015": {"n": summary["regime-2015"]["n"], "metric": "accuracy"},
            "outcome-2015": {"n": summary["outcome-2015"]["n"], "metric": "auroc"},
            "routing-2025": {"n": summary["routing-2025"]["n"], "metric": "accuracy"},
            "regime-shared": {"n": shared["n"], "metric": "accuracy"},
            "intervals": "paired bootstrap over items, model minus TF-IDF, 2,000 resamples",
        },
        "h1_sr01_open_minus_claude": e5["h1_sr01_open_minus_claude"],
        "sources": [
            "engine/results/real/models/summary.json",
            "engine/results/real/open_models/cfpb/regime_completed/summary.json",
            "engine/results/real/episodes/episodes.json",
            "engine/results/real/open_models/episodes/",
            "engine/results/real/open_models/e5_episodes.json",
            "engine/results/real/incentives/e4.json",
        ],
        "checks": checks,
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ENGINE.parent)}: {len(rows)} rows")
    for c in checks:
        print("  ok:", c)


if __name__ == "__main__":
    main()
