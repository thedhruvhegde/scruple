"""E6 analysis: leaderboards per environment, failure taxonomy, notable behaviours.

    uv run python results/llm_sims/analyse.py

Reads every `results/llm_sims/runs/<model>/<env>/<strategy>/items/*.json`, re-grades
nothing (the records hold the replayed outcomes), replays the calls once more only to
recover the verifiers' explanations for the notable-behaviour list, and writes
`results/llm_sims/llm_sims.json` and `results/llm_sims/TABLES.md`.

Provider-limited episodes (an HTTP 413: one request alone over the model's
input-tokens-per-minute limit, which no retry can fix) are scored as lapses in the
main tables, as pre-registered, and also reported separately.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scruple.suite.envs import get_adapter, replay
from scruple.suite.runner import ItemRecord
from scruple.suite.stats import boot_mean, paired_diff, wilson
from scruple.suite.taxonomy import category

ROOT = Path(__file__).parent
RUNS = ROOT / "runs"
ENV_ORDER = ("dispute", "recon", "aml", "ach", "payables", "commerce")
NOTABLE = ("fabrication", "self_report", "injection", "scope")


def load() -> dict[tuple[str, str, str], list[ItemRecord]]:
    cells: dict[tuple[str, str, str], list[ItemRecord]] = {}
    for d in sorted(RUNS.glob("*/*/*/items")):
        model, env, strat = d.parent.parent.parent.name.replace("__", "/"), d.parent.parent.name, d.parent.name
        recs = [ItemRecord.model_validate_json(p.read_text()) for p in sorted(d.glob("*.json"))]
        if recs:
            cells[(model, env, strat)] = sorted(recs, key=lambda r: r.item.seed)
    return cells


def limited(r: ItemRecord) -> bool:
    return (r.play.detail or "").startswith("HTTP 413")


def card_of(env: str, ep: Any) -> Any:
    if env in ("dispute", "commerce"):
        return ep.score.scorecard
    from scruple.finops.envs import get_env

    return get_env(env).verifier().score(ep.record())


def row(label: str, recs: list[ItemRecord], refs: dict[str, Any] | None, honest: str | None) -> dict[str, Any]:
    n = len(recs)
    k = sum(r.outcome.gate for r in recs)
    m, lo, hi = boot_mean([r.outcome.reward for r in recs])
    out: dict[str, Any] = {
        "system": label,
        "n": n,
        "gate": k,
        "gate_ci": wilson(k, n),
        "reward": round(m, 3),
        "reward_ci": [round(lo, 3), round(hi, 3)],
        "hard": dict(Counter(h for r in recs for h in r.outcome.hard_failures).most_common()),
        "taxonomy": dict(Counter(category(h) for r in recs for h in set(r.outcome.hard_failures)).most_common()),
        "stops": dict(Counter(r.play.stop for r in recs)),
        "provider_limited": sum(limited(r) for r in recs),
        "tokens_in": sum(r.play.input_tokens for r in recs),
        "tokens_out": sum(r.play.output_tokens for r in recs),
        "episode_s": round(sum(r.play.latency_s for r in recs) / n, 1) if n else 0,
        "request_s": None,
        "rejected": sum(r.play.rejected_turns for r in recs),
        "elisions": sum(r.play.elisions for r in recs),
        "replay_mismatch": sum(not r.replay_match for r in recs),
    }
    rl = [t for r in recs for t in r.play.request_latency_s]
    if rl:
        out["request_s"] = round(sorted(rl)[len(rl) // 2], 2)
    if recs and "cost_usd" in recs[0].outcome.metrics:
        out["cost"] = round(sum(r.outcome.metrics["cost_usd"] for r in recs) / n, 2)
        out["fair"] = sum(bool(r.outcome.metrics.get("fair_outcome")) for r in recs)
        out["won"] = sum(bool(r.outcome.metrics.get("won")) for r in recs)
    if refs is not None and honest is not None and recs:
        hr = [refs[r.item.seed][honest] for r in recs]
        d = paired_diff([r.outcome.reward for r in recs], [h.reward for h in hr])
        out["vs_ref"] = [round(x, 3) for x in d]
        if "cost" in out:
            c = paired_diff([r.outcome.metrics["cost_usd"] for r in recs], [h.metrics["cost_usd"] for h in hr])
            out["cost_vs_ref"] = [round(x, 2) for x in c]
    return out


def main() -> None:
    cells = load()
    report: dict[str, Any] = {"cells": {}, "notable": [], "provider_limited": [], "synthetic": True}
    md: list[str] = []
    by_env: dict[str, list[tuple[str, str, list[ItemRecord]]]] = defaultdict(list)
    for (model, env, strat), recs in cells.items():
        by_env[env].append((model, strat, recs))
    for env in ENV_ORDER:
        if env not in by_env:
            continue
        a = get_adapter(env)
        seeds = sorted({r.item.seed for _, _, recs in by_env[env] for r in recs})
        cts = {r.item.seed: r.item.case_type for _, _, recs in by_env[env] for r in recs}
        refs = {s: a.reference_outcomes(s, cts[s]) for s in seeds}
        rows = [row(f"{m} / {s}", recs, refs, a.honest_ref) for m, s, recs in sorted(by_env[env])]
        for name in refs[seeds[0]]:
            fake = [
                ItemRecord(item=r.item, play=r.play.model_copy(update={"input_tokens": 0, "output_tokens": 0, "latency_s": 0.0, "request_latency_s": ()}), outcome=refs[r.item.seed][name], replay_match=True)
                for r in sorted({x.item.seed: x for _, _, recs in by_env[env] for x in recs}.values(), key=lambda x: x.item.seed)
            ]
            rr = row(f"ref:{name}", fake, None, None)
            for k in ("stops", "tokens_in", "tokens_out", "episode_s", "request_s", "rejected", "elisions", "provider_limited", "replay_mismatch"):
                rr.pop(k, None)
            rows.append(rr)
        report["cells"][env] = rows
        md.append(f"## {env} ({a.title})\n")
        md.append(f"Seeds {seeds[0]}-{seeds[-1]}" + ("" if env == "dispute" else f", case types {sorted(set(cts.values()))[:2]}") + ".\n")
        head = "| system | n | gate pass (95% CI) | mean reward (95% CI) | reward vs " + a.honest_ref + " ref (95% CI) |"
        sep = "|---|---|---|---|---|"
        if env == "dispute":
            head += " mean cost $ | cost vs evidence (95% CI) | won/partial | fair (OUT-01) |"
            sep += "---|---|---|---|"
        head += " hard failures | tokens in/out | s/episode |"
        sep += "---|---|---|"
        md += [head, sep]
        for r in rows:
            g = f"{r['gate']}/{r['n']} [{r['gate_ci'][0]:.2f}, {r['gate_ci'][1]:.2f}]"
            rw = f"{r['reward']:.3f} [{r['reward_ci'][0]:.3f}, {r['reward_ci'][1]:.3f}]"
            vs = f"{r['vs_ref'][0]:+.3f} [{r['vs_ref'][1]:+.3f}, {r['vs_ref'][2]:+.3f}]" if "vs_ref" in r else ""
            line = f"| {r['system']} | {r['n']} | {g} | {rw} | {vs} |"
            if env == "dispute":
                cv = f"{r['cost_vs_ref'][0]:+.2f} [{r['cost_vs_ref'][1]:+.2f}, {r['cost_vs_ref'][2]:+.2f}]" if "cost_vs_ref" in r else ""
                line += f" {r.get('cost', '')} | {cv} | {r.get('won', '')}/{r['n']} | {r.get('fair', '')}/{r['n']} |"
            hf = ", ".join(f"{k} x{v}" for k, v in r["hard"].items()) or "none"
            if r.get("provider_limited"):
                hf += f" (+{r['provider_limited']} provider-limited lapses)"
            tok = f"{r['tokens_in']}/{r['tokens_out']}" if "tokens_in" in r else ""
            line += f" {hf} | {tok} | {r.get('episode_s', '')} |"
            md.append(line)
        md.append("")
    # notable behaviours, with the verifiers' own explanations
    for (model, env, strat), recs in cells.items():
        a = get_adapter(env)
        for r in recs:
            cats = {category(h) for h in r.outcome.hard_failures} | {
                category(s) for s in r.outcome.soft_failures if category(s) == "injection"
            }
            if limited(r):
                report["provider_limited"].append({"model": model, "env": env, "strategy": strat, "seed": r.item.seed, "detail": (r.play.detail or "")[:160]})
            if not cats & set(NOTABLE):
                continue
            ep = replay(a, r.item.seed, r.item.case_type, r.play.calls)
            card = card_of(env, ep)
            why = {
                c.id: c.explanation[:300]
                for c in card.checks
                if not c.passed and category(c.id) in NOTABLE
            }
            report["notable"].append(
                {
                    "model": model,
                    "env": env,
                    "strategy": strat,
                    "seed": r.item.seed,
                    "episode_id": r.play.episode_id,
                    "categories": sorted(cats & set(NOTABLE)),
                    "checks": why,
                    "uploads": [c["args"] for c in r.play.calls if c.get("tool") == "upload_evidence"][:2],
                    "submit": next((c["args"] for c in r.play.calls if c.get("tool") == "submit_decision"), None),
                }
            )
    md.append("## Notable behaviours (hard failures in fabrication, self-report, injection, scope)\n")
    for x in report["notable"]:
        md.append(f"- {x['model']} / {x['strategy']} / {x['env']} seed {x['seed']} ({x['episode_id']}): " + "; ".join(f"{k}: {v}" for k, v in x["checks"].items()))
    md.append("")
    md.append(f"Provider-limited lapses (HTTP 413): {len(report['provider_limited'])}")
    (ROOT / "llm_sims.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n")
    (ROOT / "TABLES.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
