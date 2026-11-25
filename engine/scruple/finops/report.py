# ruff: noqa: E501  (markdown table rows)
"""Run every FINOPS_SUITE.md experiment and write `results.json` and `results.md`.

`run_all(out, seeds=1000, train=200)`: baselines and caught rates on dev seeds
`0..seeds-1`; exploit coverage on the same seeds; marginal incentives on the same
seeds; the hill climb on train seeds `50000..50000+train-1` (disjoint), its winners
then re-scored on the dev seeds; throughput on this machine. Deterministic except the
timings.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from scruple.finops import experiments as xp
from scruple.finops.envs import ENVS, get_env
from scruple.finops.exploits import coverage, render
from scruple.finops.parallel import bench, default_workers
from scruple.finops.policies import AmlKnobs, AmlPolicy, ReconKnobs, ReconPolicy

TRAIN0 = 50_000


def _knobs_from(env: str, key: str) -> Any:
    on = {} if key == "honest" else dict.fromkeys(key.split("+"), True)
    return ReconPolicy(ReconKnobs(**on)) if env == "recon" else AmlPolicy(AmlKnobs(**on))


def run_all(
    out: Path, *, seeds: int = 1000, train: int = 200, workers: int | None = None
) -> dict[str, Any]:
    w = workers or default_workers()
    out.mkdir(parents=True, exist_ok=True)
    dev = range(seeds)
    res: dict[str, Any] = {
        "seeds": [0, seeds - 1],
        "train": [TRAIN0, TRAIN0 + train - 1],
        "workers": w,
    }
    t0 = time.perf_counter()
    res["baselines"] = xp.baselines(dev, workers=w)
    res["caught"] = xp.caught(dev, workers=w)
    res["incentives"] = {env: xp.incentives(env, dev, workers=w) for env in ("recon", "aml")}
    climbs: dict[str, Any] = {}
    for env in ("recon", "aml"):
        e = get_env(env)
        for obj in ("proxy", *(v.id for v in e.verifiers())):
            c = xp.climb(env, obj, range(TRAIN0, TRAIN0 + train), workers=w)
            rows = xp.play_many(env, _knobs_from(env, c["best"]), dev, workers=w)
            c["dev"] = {
                v.id: round(sum(r[v.id][0] for r in rows) / len(rows), 4) for v in e.verifiers()
            } | {"proxy": round(sum(r["proxy"] for r in rows) / len(rows), 4)}
            climbs[f"{env}:{obj}"] = c
    res["climb"] = climbs
    cov = coverage(dev, workers=w)
    res["coverage"] = [r.model_dump() for r in cov]
    res["throughput"] = [bench(env, 400, workers=w) for env in ENVS]
    res["seconds"] = round(time.perf_counter() - t0, 1)
    (out / "results.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (out / "results.md").write_text(markdown(res, render(cov)))
    return res


def _ci(d: dict[str, Any]) -> str:
    return f"{d['mean']:.3f} [{d['ci95'][0]:.3f}, {d['ci95'][1]:.3f}]"


def markdown(res: dict[str, Any], coverage_md: str) -> str:
    lines = [
        f"Dev seeds {res['seeds'][0]}-{res['seeds'][1]}; train seeds {res['train'][0]}-{res['train'][1]}.",
        "",
    ]
    for env, pols in res["baselines"].items():
        vids = [v.id for v in get_env(env).verifiers()]
        lines += [f"### {env}: reward by policy (mean [95% bootstrap CI])", ""]
        lines.append(
            "| policy | "
            + " | ".join(vids)
            + " | gate rate (default) | Spearman vs difficulty | lapsed |"
        )
        lines.append("|---" * (len(vids) + 4) + "|")
        dv = get_env(env).default_verifier
        for p, d in pols.items():
            lines.append(
                f"| {p} | "
                + " | ".join(_ci(d[v]) for v in vids)
                + f" | {d[dv]['gate_rate']:.3f} | {d[dv]['spearman_vs_difficulty']:+.3f} | {d['lapsed']} |"
            )
        lines += ["", f"Reward by nominal difficulty ({dv}):", ""]
        bins = list(next(iter(pols.values()))[dv]["by_difficulty"])
        lines.append("| policy | " + " | ".join(bins) + " |")
        lines.append("|---" * (len(bins) + 1) + "|")
        for p, d in pols.items():
            lines.append(
                f"| {p} | " + " | ".join(f"{d[dv]['by_difficulty'][b]:.2f}" for b in bins) + " |"
            )
        lines.append("")
    lines += ["### Honest false positives and hacker catch rate (Wilson 95%)", ""]
    lines.append(
        "| env | verifier | honest episodes with a hard failure | hacker hard-caught | hacker below honest (same seed) | hacker / honest reward |"
    )
    lines.append("|---|---|---|---|---|---|")
    for env, vs in res["caught"].items():
        for vid, d in vs.items():
            fp, hc, lo = (
                d["honest_false_positive"],
                d["hacker_hard_caught"],
                d["hacker_below_honest"],
            )
            lines.append(
                f"| {env} | {vid} | {fp[0]}/{fp[1]} [{fp[3]:.4f}, {fp[4]:.4f}] | {hc[0]}/{hc[1]} = {hc[2]:.3f} "
                f"[{hc[3]:.3f}, {hc[4]:.3f}] | {lo[0]}/{lo[1]} = {lo[2]:.3f} | {d['hacker_reward']:.3f} / {d['honest_reward']:.3f} |"
            )
    lines += ["", "### Marginal exploit incentives (paired delta in reward, 95% bootstrap CI)", ""]
    for env, bases in res["incentives"].items():
        vids = [v.id for v in get_env(env).verifiers()]
        for base, d in bases.items():
            lines.append(
                f"{env}, base = {base} (base reward "
                + ", ".join(f"{v} {d['base'][v]:.3f}" for v in vids)
                + "):"
            )
            lines.append("")
            lines.append(
                "| knob | "
                + " | ".join(f"{v} delta" for v in vids)
                + " | proxy delta | hard-fail rate (default) |"
            )
            lines.append("|---" * (len(vids) + 3) + "|")
            dv = get_env(env).default_verifier
            for knob, x in d.items():
                if knob == "base":
                    continue
                lines.append(
                    f"| {knob} | "
                    + " | ".join(
                        f"{x[v]['delta']:+.3f} [{x[v]['ci95'][0]:+.3f}, {x[v]['ci95'][1]:+.3f}]"
                        for v in vids
                    )
                    + f" | {x[dv]['proxy_delta']:+.3f} | {x[dv]['hard_rate']:.3f} |"
                )
            lines.append("")
    lines += ["### Hill climb over the knob grid (train seeds; winner re-scored on dev seeds)", ""]
    lines.append(
        "| env:objective | winner | train value | exploit knobs on | dev: proxy | dev: verifiers |"
    )
    lines.append("|---|---|---|---|---|---|")
    for k, c in res["climb"].items():
        dev = ", ".join(f"{v} {x:.3f}" for v, x in c["dev"].items() if v != "proxy")
        lines.append(
            f"| {k} | {c['best']} | {c['best_value']:.3f} | {', '.join(c['exploits_on']) or '-'} | {c['dev']['proxy']:.3f} | {dev} |"
        )
    lines += ["", "### Exploit coverage", "", coverage_md, "", "### Throughput (this machine)", ""]
    lines.append(
        "| env | policy | n | workers | episodes/s serial | episodes/s parallel | grades/s serial | grades/s parallel |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in res["throughput"]:
        lines.append(
            f"| {b['env']} | {b['policy']} | {b['n']} | {b['workers']} | {b['episodes_per_s_serial']} | "
            f"{b['episodes_per_s_parallel']} | {b['grades_per_s_serial']} | {b['grades_per_s_parallel']} |"
        )
    lines += ["", f"Wall time {res['seconds']} s.", ""]
    return "\n".join(lines)


__all__ = ["markdown", "run_all"]
