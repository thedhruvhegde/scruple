"""`scruple eval run --env recon|aml|ach|payables|commerce` (and `dispute` from Python):
one agent and strategy over a seed list, resumable, graded by replay, compared with
the environment's scripted references on the same tasks.

Run directory:

    OUT/manifest.json              env, agent, strategy, seeds, difficulty, loop
    OUT/items/<env>-s<seed>.json   one `ItemRecord` per seed (calls, tokens, latency,
                                   replayed outcome)
    OUT/transcripts/*.jsonl        request/response bodies (real API runs)
    OUT/lapsed/                    transient attempts kept before a replay
    OUT/summary.json, SUMMARY.md   the agent and the references, with intervals

Before this module, `eval run --env` for the finops environments wrote nothing until
every seed had finished (a crash or a rate limit lost the whole run), ignored the
strategy, `--price-*` and `--no-seed`, and had no reference comparison; commerce
could not be run at all.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scruple.suite.agents import FINOPS_LOOP, Agent
from scruple.suite.envs import get_adapter
from scruple.suite.runner import Item, ItemRecord, load_item_records, run_items
from scruple.suite.stats import boot_mean, paired_diff, wilson


def items_for(
    env: str, seeds: Sequence[int], strategy: str, difficulty: float | None
) -> list[Item]:
    a = get_adapter(env)
    out = []
    for s in seeds:
        ct = (
            None
            if (env == "dispute" or difficulty is None)
            else a.case_type_at(difficulty, random.Random(s))
        )
        out.append(
            Item(
                scenario_id=f"{env}-s{s}",
                env=env,
                seed=s,
                case_type=ct,
                strategy=strategy,
                difficulty=difficulty,
            )
        )
    return out


def run_env_eval(
    env: str,
    agent: Agent,
    strategy: str,
    seeds: Sequence[int],
    out: Path,
    *,
    difficulty: float | None = 0.5,
    concurrency: int = 1,
    progress: Callable[[ItemRecord], None] | None = None,
    transcripts: bool = True,
) -> dict[str, Any]:
    a = get_adapter(env)
    a.system_prompt(strategy)  # refuse an unknown strategy before any request
    out.mkdir(parents=True, exist_ok=True)
    man: dict[str, Any] = {
        "env": env,
        "agent": agent.label,
        "strategy": strategy,
        "difficulty": None if env == "dispute" else difficulty,
        "loop": None if env == "dispute" else FINOPS_LOOP.model_dump(mode="json"),
        "synthetic": True,
    }
    mp = out / "manifest.json"
    if mp.exists():
        old = json.loads(mp.read_text())
        if any(old.get(k) != man[k] for k in ("env", "agent", "strategy", "difficulty")):
            raise ValueError(f"{out} holds a different run ({old.get('agent')}); use a new --out")
        seeds = sorted(set(seeds) | set(old.get("seeds", [])))
    man["seeds"] = sorted(seeds)
    mp.write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")
    run_items(
        items_for(env, seeds, strategy, difficulty),
        agent,
        out,
        concurrency=concurrency,
        progress=progress,
        transcripts=transcripts,
    )
    return write_env_summary(out)


def _system_rows(label: str, outs: list[Any], recs: list[ItemRecord] | None) -> dict[str, Any]:
    n = len(outs)
    k = sum(o.gate for o in outs)
    r = boot_mean([o.reward for o in outs])
    hard = Counter(h for o in outs for h in o.hard_failures)
    row: dict[str, Any] = {
        "system": label,
        "n": n,
        "gate_pass": k,
        "gate_rate_ci": wilson(k, n),
        "mean_reward": r[0],
        "reward_ci": [r[1], r[2]],
        "hard_failures": dict(hard.most_common()),
        "soft_failures": dict(Counter(s for o in outs for s in o.soft_failures).most_common()),
    }
    costs = [o.metrics.get("cost_usd") for o in outs]
    if costs and all(c is not None for c in costs):
        row["mean_cost_usd"] = round(sum(costs) / n, 2)
        row["fair_outcome"] = sum(bool(o.metrics.get("fair_outcome")) for o in outs)
        row["won_or_partial"] = sum(bool(o.metrics.get("won")) for o in outs)
    if recs is not None:
        p = [x.play for x in recs]
        row["stops"] = dict(Counter(x.stop for x in p))
        row["input_tokens"] = sum(x.input_tokens for x in p)
        row["output_tokens"] = sum(x.output_tokens for x in p)
        row["requests"] = sum(x.requests for x in p)
        row["rejected_turns"] = sum(x.rejected_turns for x in p)
        row["elisions"] = sum(x.elisions for x in p)
        lat = [x.latency_s for x in p]
        row["mean_episode_latency_s"] = round(sum(lat) / n, 1) if n else 0.0
        rl = [t for x in p for t in x.request_latency_s]
        row["mean_request_latency_s"] = round(sum(rl) / len(rl), 2) if rl else None
        row["replay_mismatches"] = sum(not x.replay_match for x in recs)
    return row


def write_env_summary(out: Path) -> dict[str, Any]:
    man = json.loads((out / "manifest.json").read_text())
    recs = sorted(load_item_records(out), key=lambda r: r.item.seed)
    env = man["env"]
    a = get_adapter(env)
    refs = {r.item.scenario_id: a.reference_outcomes(r.item.seed, r.item.case_type) for r in recs}
    label = f"{man['agent']}/{man['strategy']}"
    rows = [_system_rows(label, [r.outcome for r in recs], recs)]
    ref_names = list(next(iter(refs.values()))) if refs else []
    for name in ref_names:
        rows.append(
            _system_rows(f"ref:{name}", [refs[r.item.scenario_id][name] for r in recs], None)
        )
    if recs:
        honest = [refs[r.item.scenario_id][a.honest_ref] for r in recs]
        d = paired_diff([r.outcome.reward for r in recs], [h.reward for h in honest])
        rows[0]["reward_vs_honest_ref"] = {"ref": a.honest_ref, "diff": d[0], "ci": [d[1], d[2]]}
        if "mean_cost_usd" in rows[0]:
            c = paired_diff(
                [r.outcome.metrics["cost_usd"] for r in recs],
                [h.metrics["cost_usd"] for h in honest],
            )
            rows[0]["cost_vs_honest_ref"] = {"ref": a.honest_ref, "diff": c[0], "ci": [c[1], c[2]]}
    summary = {
        "manifest": man,
        "rows": rows,
        "synthetic": True,
        "real_model": any(r.play.real_model for r in recs),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    (out / "SUMMARY.md").write_text(summary_markdown(summary))
    return summary


def summary_markdown(summary: dict[str, Any]) -> str:
    man = summary["manifest"]
    seeds = man["seeds"]
    span = f"{seeds[0]}..{seeds[-1]}" if seeds else "-"
    lines = [
        f"# {man['agent']}/{man['strategy']} on {man['env']}",
        "",
        f"Seeds {span} ({len(seeds)}); difficulty {man['difficulty']}. "
        "Synthetic cases; graded by replay. "
        + ("" if summary["real_model"] else "NOT a real model (harness check). "),
        "",
        "| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref "
        "| top hard failures |",
        "|---|---|---|---|---|---|",
    ]
    for r in summary["rows"]:
        lo, hi = r["gate_rate_ci"]
        vs = r.get("reward_vs_honest_ref")
        vs_s = f"{vs['diff']:+.3f} [{vs['ci'][0]:+.3f}, {vs['ci'][1]:+.3f}]" if vs else ""
        hf = ", ".join(f"{k} x{v}" for k, v in list(r["hard_failures"].items())[:4]) or "none"
        rci = r["reward_ci"]
        lines.append(
            f"| {r['system']} | {r['n']} | {r['gate_pass']}/{r['n']} [{lo:.2f}, {hi:.2f}] | "
            f"{r['mean_reward']:.3f} [{rci[0]:.3f}, {rci[1]:.3f}] | {vs_s} | {hf} |"
        )
    top = summary["rows"][0] if summary["rows"] else None
    if top and "stops" in top:
        lines += [
            "",
            f"Stops: {', '.join(f'{k} {v}' for k, v in top['stops'].items())}. "
            f"Tokens: {top['input_tokens']} in, {top['output_tokens']} out; "
            f"requests {top['requests']}; "
            f"mean episode wall time {top['mean_episode_latency_s']} s.",
        ]
    return "\n".join(lines) + "\n"


__all__ = ["items_for", "run_env_eval", "summary_markdown", "write_env_summary"]
