"""E5 analysis: open models (API) next to the Claude subagent runs, same seeds.

Usage:
    uv run python results/real/open_models/analyse_e5.py

Reads the open-model run directories (results/real/open_models/episodes/*), whose
per-seed records hold the scored row of EVERY episode, lapsed ones included, and
re-scores the three stored Claude runs (results/real/episodes/store-*) with the
current verifiers by replay, the way scripts/model_episodes_report.py does. Writes
results/real/open_models/e5_episodes.json and E5_EPISODES.md:

- one row per system: n, mean cost and its paired bootstrap against the evidence
  reference, gate pass, SR-01 failures, OUT-01 passes, won/partial, hard failures;
- H1: pooled open-model minus pooled Claude SR-01 failure rate, paired by seed,
  2,000 bootstrap resamples over seeds, seed 0.

Synthetic cases; real model behaviour. Deterministic for fixed inputs.
"""

from __future__ import annotations

import importlib.util
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CLAUDE = ("haiku", "sonnet", "opus")
N_BOOT = 2000


def _report_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "model_episodes_report", ROOT / "scripts" / "model_episodes_report.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def open_rows() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    for d in sorted((HERE / "episodes").iterdir()):
        recs = [json.loads(p.read_text()) for p in sorted((d / "runs").glob("seed-*.json"))]
        if not recs:
            continue
        label = d.name
        stops = Counter(r["stop"] for r in recs)
        meta[label] = {
            "model": recs[0]["model"],
            "stops": dict(stops),
            "rejected_turns": sum(r.get("rejected_turns", 0) for r in recs),
            "input_tokens": sum(r["input_tokens"] or 0 for r in recs),
            "output_tokens": sum(r["output_tokens"] or 0 for r in recs),
            "turns": sum(r["turns"] for r in recs),
        }
        for r in recs:
            row = dict(r["row"])
            row["system"] = label
            row["stop"] = r["stop"]
            rows.append(row)
    return rows, meta


def sensitivity() -> dict[str, dict[str, Any]]:
    """Two checks on the pre-registered scoring, per open-model run (not replacements):

    - `no_rejected`: replay each episode without the calls to tools that do not
      exist. On Groq those only come from generations the API refused
      (`rejected_turns`), mostly gpt-oss submits addressed to `json` or
      `commentary`. The model still saw the error; only the episode forgets the call.
    - `no_quota_lapses`: drop episodes that ended on `http_error`/`timeout`, which
      here are the API's per-minute token limits (HTTP 413/429), not the model.
    """
    from scruple.agents.dispute_episode import DisputeEpisode
    from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES
    from scruple.eval.scoring import row as score_row

    out: dict[str, dict[str, Any]] = {}
    for d in sorted((HERE / "episodes").iterdir()):
        recs = [json.loads(p.read_text()) for p in sorted((d / "runs").glob("seed-*.json"))]
        if not recs:
            continue
        clean = []
        for r in recs:
            calls = [(c["tool"], c["args"]) for c in r["calls"] if c["tool"] in DISPUTE_TOOL_NAMES]
            ep = DisputeEpisode.replay(r["seed"], None, calls)
            score = ep.score if ep.score is not None else ep.lapse(r["stop"])
            clean.append(score_row(d.name, json.loads(score.model_dump_json())))
        kept = [r["row"] for r in recs if r["stop"] not in ("http_error", "timeout")]

        def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "n": len(rows),
                "gate_pass": sum(bool(x["gate"]) for x in rows),
                "sr01_fail": sum(sr_fail(x) for x in rows),
                "fair_outcome": sum(bool(x["fair_outcome"]) for x in rows),
                "hard_failures": dict(Counter(f for x in rows for f in x["hard_failures"])),
            }

        out[d.name] = {"no_rejected": summary(clean), "no_quota_lapses": summary(kept)}
    return out


def _boot_mean(d: list[float]) -> tuple[float, float, float]:
    rng = random.Random(0)
    boots = sorted(sum(d[rng.randrange(len(d))] for _ in d) / len(d) for _ in range(N_BOOT))
    return sum(d) / len(d), boots[int(0.025 * N_BOOT)], boots[int(0.975 * N_BOOT) - 1]


def sr_fail(row: dict[str, Any]) -> int:
    return int("SR-01" in row["hard_failures"])


def main() -> None:
    rep = _report_module()
    orows, meta = open_rows()
    crows: list[dict[str, Any]] = []
    for name in CLAUDE:
        crows += rep._store_rows(name, str(ROOT / "results/real/episodes" / f"store-{name}"))
    seeds = sorted({r["seed"] for r in orows} | {r["seed"] for r in crows})
    refs = rep._policy_rows(seeds)
    rows = crows + orows + refs
    systems = list(dict.fromkeys(r["system"] for r in rows))
    by = {s: {r["seed"]: r for r in rows if r["system"] == s} for s in systems}
    ev = by["ref:evidence"]

    table = []
    for s in systems:
        rs = by[s]
        n = len(rs)
        cost = sum(r["cost_usd"] for r in rs.values()) / n
        if s == "ref:evidence":
            cmp = None
        else:
            cmp = _boot_mean([rs[k]["cost_usd"] - ev[k]["cost_usd"] for k in sorted(rs)])
        fails = Counter(f for r in rs.values() for f in r["hard_failures"]).most_common(5)
        table.append(
            {
                "system": s,
                "harness": "claude-code-subagent"
                if s in CLAUDE
                else ("in-process" if s.startswith("ref:") else "api"),
                "n": n,
                "mean_cost_usd": round(cost, 2),
                "cost_vs_evidence": None if cmp is None else [round(x, 2) for x in cmp],
                "gate_pass": sum(bool(r["gate"]) for r in rs.values()),
                "sr01_fail": sum(sr_fail(r) for r in rs.values()),
                "fair_outcome": sum(bool(r["fair_outcome"]) for r in rs.values()),
                "won_partial": sum(r["resolution"] in ("won", "partial") for r in rs.values()),
                "mean_reward": round(sum(r["reward"] for r in rs.values()) / n, 3),
                "hard_failures": dict(fails),
            }
        )

    def h1_for(opens: list[str]) -> dict[str, Any] | None:
        common = [k for k in seeds if all(k in by[s] for s in (*opens, *CLAUDE))]
        diffs = [
            sum(sr_fail(by[s][k]) for s in opens) / len(opens)
            - sum(sr_fail(by[s][k]) for s in CLAUDE) / len(CLAUDE)
            for k in common
        ]
        if not diffs:
            return None
        m, lo, hi = _boot_mean(diffs)
        return {
            "open": opens,
            "seeds": common,
            "diff": round(m, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
        }

    # Pre-registered: all open models on the seeds all of them finished. Secondary:
    # the open models that finished every seed.
    h1 = {
        "all_open_models": h1_for(list(meta)),
        "complete_open_models": h1_for([s for s in meta if len(by[s]) == len(seeds)]),
    }
    out = {
        "note": "Synthetic dispute cases (FULL_MIX); real model behaviour. Claude rows: "
        "Claude Code subagents via the scruple episode CLI, re-scored by replay. Open "
        "models: raw API via scruple eval run (Groq).",
        "seeds": seeds,
        "systems": table,
        "open_runs": meta,
        "h1_sr01_open_minus_claude": h1,
    }
    sens = sensitivity()
    out["sensitivity"] = sens
    (HERE / "e5_episodes.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")

    lines = [
        "# E5 episodes: open models (API) and Claude (subagents), same seeds",
        "",
        f"Seeds {seeds[0]}-{seeds[-1]}. Synthetic cases; real model behaviour. "
        "Cost = merchant net loss in USD. Paired bootstrap over seeds, 2,000 resamples.",
        "",
        "| system | harness | n | mean cost $ | vs evidence (95% ci) | gate pass | SR-01 fail "
        "| fair (OUT-01) | won/partial | mean reward | hard failures |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in table:
        c = t["cost_vs_evidence"]
        ctxt = "" if c is None else f"{c[0]:+.2f} [{c[1]:+.2f}, {c[2]:+.2f}]"
        ftxt = ", ".join(f"{k} x{v}" for k, v in t["hard_failures"].items()) or "none"
        n = t["n"]
        lines.append(
            f"| {t['system']} | {t['harness']} | {n} | {t['mean_cost_usd']:.2f} | {ctxt} "
            f"| {t['gate_pass']}/{n} | {t['sr01_fail']}/{n} | {t['fair_outcome']}/{n} "
            f"| {t['won_partial']}/{n} | {t['mean_reward']:.3f} | {ftxt} |"
        )
    for key, v in h1.items():
        if v is not None:
            lines += [
                "",
                f"H1 ({key}: {', '.join(v['open'])}), SR-01 failure rate, pooled open minus "
                f"pooled Claude, paired over {len(v['seeds'])} seeds: {v['diff']:+.3f} "
                f"[{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}].",
            ]
    lines += [
        "",
        "Sensitivity (not the pre-registered scoring): gate pass / SR-01 fail / OUT-01 pass.",
        "",
        "| system | as scored | without calls to non-existent tools | quota lapses excluded |",
        "|---|---|---|---|",
    ]
    for s, v in sens.items():
        t = next(x for x in table if x["system"] == s)
        a, b = v["no_rejected"], v["no_quota_lapses"]
        lines.append(
            f"| {s} | {t['gate_pass']}/{t['n']} / {t['sr01_fail']} / {t['fair_outcome']} "
            f"| {a['gate_pass']}/{a['n']} / {a['sr01_fail']} / {a['fair_outcome']} "
            f"| {b['gate_pass']}/{b['n']} / {b['sr01_fail']} / {b['fair_outcome']} |"
        )
    lines += ["", "Open-model runs:", ""]
    for s, m in meta.items():
        lines.append(
            f"- {s} ({m['model']}): stops {m['stops']}, rejected generations "
            f"{m['rejected_turns']}, {m['turns']} turns, tokens {m['input_tokens']} in / "
            f"{m['output_tokens']} out."
        )
    (HERE / "E5_EPISODES.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
