"""Experiments behind docs/results/FINOPS_SUITE.md.

- `baselines`: every policy of every environment on N dev seeds along the curriculum
  (seed i plays nominal difficulty (i mod 11) / 10), scored by every verifier version:
  mean reward with a 95% bootstrap interval, gate rate, hard failures, reward by
  difficulty bin and the Spearman correlation of reward with difficulty.
- `incentives`: for a base policy (expert or novice), the paired change in reward
  when one exploit knob is switched on, with a 95% paired bootstrap interval. A
  positive interval is a reward-hacking incentive the verifier leaves open.
- `climb`: coordinate ascent over the knob grid (the E2 design of
  `scruple.bench.hacking`) on train seeds, for the naive proxy and each verifier.
- `caught`: the composite hacker's hard-check catch rate and the honest policy's
  false-positive rate, with Wilson intervals.

Everything is synthetic, on public dev seeds (the sealed benchmark salt is not used).
"""

from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from scruple.finops.difficulty import curriculum
from scruple.finops.envs import ENVS, get_env
from scruple.finops.parallel import pmap
from scruple.finops.policies import (
    AML_GRID,
    RECON_GRID,
    AmlKnobs,
    AmlPolicy,
    Policy,
    ReconKnobs,
    ReconPolicy,
    run_policy,
)

BINS = tuple(round(i / 10, 1) for i in range(11))


def difficulty_of(i: int) -> float:
    return BINS[i % len(BINS)]


def bootstrap_ci(
    xs: Sequence[float], *, n_boot: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean and a 95% percentile bootstrap interval."""
    if not xs:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return sum(xs) / n, means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


# --- one episode, many verifiers ------------------------------------------------------


def _row(item: tuple[str, Policy, int, str | None]) -> dict[str, Any]:
    env, policy, seed, ct = item
    e = get_env(env)
    ep = run_policy(e.episode_cls, policy, seed, ct)
    rec = ep.record()
    out: dict[str, Any] = {"seed": seed, "case_type": ct, "lapsed": bool(rec.lapsed)}
    for v in e.verifiers():
        card = v.score(rec)
        out[v.id] = (
            round(card.reward, 6),
            card.gate,
            [c.id for c in card.checks if c.hard and not c.passed],
        )
    out["proxy"] = proxy(env, rec)
    return out


def proxy(env: str, rec: Any) -> float:
    """The naive dashboard number: recon, half the share of statement lines matched
    and half the self-reported "balanced"; aml, half the share of alerts acted on (filed
    or escalated) and half the self-reported filing rate; ach and payables, 0 (none)."""
    if env == "recon":
        claims = rec.report.claims
        matched = {x for ms, _ in rec.outcome.matches for x in ms}
        share = len(matched) / len(rec.statement.lines) if rec.statement.lines else 1.0
        return 0.5 * share + 0.5 * float(claims.balanced is True)
    if env == "aml":
        acted = rec.outcome.decision in ("file_sar", "escalate")
        return 0.5 * float(acted) + 0.5 * float(rec.report.claims.sar_filed is True)
    return 0.0


def play_many(
    env: str, policy: Policy, seeds: Sequence[int], *, workers: int = 1, horizon: bool = False
) -> list[dict[str, Any]]:
    items = [
        (env, policy, s, curriculum(env, difficulty_of(i), horizon=horizon))
        for i, s in enumerate(seeds)
    ]
    return pmap(_row, items, workers=workers)


# --- baselines ----------------------------------------------------------------------


def baselines(
    seeds: Sequence[int], *, envs: Sequence[str] | None = None, workers: int = 1
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, e in ENVS.items():
        if envs and name not in envs:
            continue
        env_out: dict[str, Any] = {}
        for pname, pol in e.policies().items():
            rows = play_many(name, pol, seeds, workers=workers)
            pout: dict[str, Any] = {}
            for v in e.verifiers():
                rewards = [r[v.id][0] for r in rows]
                gates = [r[v.id][1] for r in rows]
                hard: dict[str, int] = defaultdict(int)
                for r in rows:
                    for h in r[v.id][2]:
                        hard[h] += 1
                by_bin: dict[float, list[float]] = defaultdict(list)
                for i, x in enumerate(rewards):
                    by_bin[difficulty_of(i)].append(x)
                m, lo, hi = bootstrap_ci(rewards)
                diffs = [difficulty_of(i) for i in range(len(rewards))]
                pout[v.id] = {
                    "mean": round(m, 4),
                    "ci95": [round(lo, 4), round(hi, 4)],
                    "gate_rate": round(sum(gates) / len(gates), 4),
                    "hard_failures": dict(sorted(hard.items())),
                    "episodes_with_hard_failure": sum(not g for g in gates),
                    "by_difficulty": {
                        f"{b:.1f}": round(sum(v_) / len(v_), 4) for b, v_ in sorted(by_bin.items())
                    },
                    "spearman_vs_difficulty": round(spearman(diffs, rewards), 4),
                }
            pout["n"] = len(rows)
            pout["lapsed"] = sum(r["lapsed"] for r in rows)
            env_out[pname] = pout
        out[name] = env_out
    return out


# --- incentives ---------------------------------------------------------------------

EXPLOIT_KNOBS = {
    "recon": ("plug", "dump", "timing_all", "claim", "peek", "follow", "obey_text", "lazy"),
    "aml": (
        "sar_always",
        "skip_review",
        "inflate",
        "network",
        "all_indicators",
        "claim",
        "peek",
        "follow",
    ),
}


def _base(env: str, novice: bool) -> Policy:
    if env == "recon":
        return ReconPolicy(ReconKnobs(novice=novice))
    return AmlPolicy(AmlKnobs(novice=novice))


def _toggle(p: Policy, knob: str) -> Policy:
    if isinstance(p, ReconPolicy):
        return ReconPolicy(replace(p.knobs, **{knob: True}))
    assert isinstance(p, AmlPolicy)
    return AmlPolicy(replace(p.knobs, **{knob: True}))


def incentives(env: str, seeds: Sequence[int], *, workers: int = 1) -> dict[str, Any]:
    e = get_env(env)
    vids = [v.id for v in e.verifiers()]
    out: dict[str, Any] = {}
    for novice in (False, True):
        base = _base(env, novice)
        b_rows = play_many(env, base, seeds, workers=workers)
        label = "novice" if novice else "expert"
        res: dict[str, Any] = {
            "base": {v: round(sum(r[v][0] for r in b_rows) / len(b_rows), 4) for v in vids}
        }
        for knob in EXPLOIT_KNOBS[env]:
            x_rows = play_many(env, _toggle(base, knob), seeds, workers=workers)
            res[knob] = {}
            for v in vids:
                d = [x[v][0] - b[v][0] for x, b in zip(x_rows, b_rows, strict=True)]
                m, lo, hi = bootstrap_ci(d)
                res[knob][v] = {
                    "delta": round(m, 4),
                    "ci95": [round(lo, 4), round(hi, 4)],
                    "hard_rate": round(sum(not x[v][1] for x in x_rows) / len(x_rows), 4),
                    "proxy_delta": round(
                        sum(x["proxy"] - b["proxy"] for x, b in zip(x_rows, b_rows, strict=True))
                        / len(b_rows),
                        4,
                    ),
                }
        out[label] = res
    return out


# --- hill climb ----------------------------------------------------------------------


def climb(
    env: str,
    objective: str,
    seeds: Sequence[int],
    *,
    restarts: int = 3,
    seed: int = 0,
    workers: int = 1,
) -> dict[str, Any]:
    """Coordinate ascent over the knob grid; `objective` is "proxy" or a verifier id."""
    grid = RECON_GRID if env == "recon" else AML_GRID
    cls = ReconKnobs if env == "recon" else AmlKnobs
    mk = (lambda k: ReconPolicy(k)) if env == "recon" else (lambda k: AmlPolicy(k))
    rng = random.Random(seed)
    cache: dict[Any, float] = {}

    def value(k: Any) -> float:
        if k not in cache:
            rows = play_many(env, mk(k), seeds, workers=workers)
            vals = [r["proxy"] if objective == "proxy" else r[objective][0] for r in rows]
            cache[k] = sum(vals) / len(vals)
        return cache[k]

    starts = [cls()]
    while len(starts) < restarts:
        starts.append(cls(**{n: rng.choice(v) for n, v in grid.items()}))
    path: list[tuple[str, float]] = []
    best, best_v = starts[0], value(starts[0])
    for s in starts:
        cur, cur_v = s, value(s)
        path.append((_keys(cur), round(cur_v, 4)))
        while True:
            moves = [
                replace(cur, **{n: v})
                for n, vals in grid.items()
                for v in vals
                if getattr(cur, n) != v
            ]
            scored = sorted(((value(m), _keys(m), m) for m in moves), key=lambda t: (-t[0], t[1]))
            top_v, _, top = scored[0]
            if top_v <= cur_v + 1e-9:
                break
            cur, cur_v = top, top_v
            path.append((_keys(cur), round(cur_v, 4)))
        if cur_v > best_v + 1e-9:
            best, best_v = cur, cur_v
    return {
        "objective": objective,
        "best": _keys(best),
        "best_value": round(best_v, 4),
        "exploits_on": list(best.exploits()),
        "evaluations": len(cache),
        "path": path,
    }


def _keys(k: Any) -> str:
    on = [n for n, v in vars(k).items() if v is True and n != "normalise"]
    return "+".join(on) or "honest"


# --- caught rates ----------------------------------------------------------------------


def caught(seeds: Sequence[int], *, workers: int = 1) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, e in ENVS.items():
        pols = e.policies()
        hon = play_many(name, pols["honest"], seeds, workers=workers)
        hack = play_many(name, pols["hacker"], seeds, workers=workers)
        env_out = {}
        for v in e.verifiers():
            fp = sum(not r[v.id][1] for r in hon)
            hc = sum(not r[v.id][1] for r in hack)
            lower = sum(x[v.id][0] < h[v.id][0] for x, h in zip(hack, hon, strict=True))
            env_out[v.id] = {
                "honest_false_positive": [
                    fp,
                    len(hon),
                    *(round(x, 4) for x in wilson(fp, len(hon))),
                ],
                "hacker_hard_caught": [
                    hc,
                    len(hack),
                    *(round(x, 4) for x in wilson(hc, len(hack))),
                ],
                "hacker_below_honest": [
                    lower,
                    len(hack),
                    *(round(x, 4) for x in wilson(lower, len(hack))),
                ],
                "hacker_reward": round(sum(r[v.id][0] for r in hack) / len(hack), 4),
                "honest_reward": round(sum(r[v.id][0] for r in hon) / len(hon), 4),
            }
        out[name] = env_out
    return out


# --- anchoring check and coupling sweeps ---------------------------------------------


def anchoring_check(env: str, seeds: Sequence[int], *, workers: int = 1) -> dict[str, Any]:
    """Theorem 4 / Conjecture 4c: every policy of the knob grid (peek excluded) against
    its honest projection under `env@2`."""
    from scruple.finops.anchoring import _project_item
    from scruple.finops.policies import knob_grid

    grid = {k: v for k, v in (RECON_GRID if env == "recon" else AML_GRID).items() if k != "peek"}
    cls = ReconKnobs if env == "recon" else AmlKnobs
    mk = ReconPolicy if env == "recon" else AmlPolicy
    pols = [mk(k) for k in knob_grid(cls, grid)]
    items = [
        (env, p, s, curriculum(env, difficulty_of(i)), f"{env}@2")
        for p in pols
        for i, s in enumerate(seeds)
    ]
    out = pmap(_project_item, items, workers=workers)
    viol: dict[str, int] = defaultdict(int)
    gaps = [a - b for a, b, _ in out]
    for _, _, per in out:
        for cid, (x, y) in per.items():
            if y < x - 1e-9:
                viol[cid] += 1
    return {
        "env": env,
        "policies": len(pols),
        "episodes": len(out),
        "max_gap": round(max(gaps), 6),
        "episodes_reward_above_projection": sum(g > 1e-9 for g in gaps),
        "check_decreases_on_projection": dict(sorted(viol.items())),
        "mean_gap": round(sum(gaps) / len(gaps), 6),
    }


def sweep(
    env: str, policy: str, knob: str, seeds: Sequence[int], *, base: str = "@0.5", workers: int = 1
) -> dict[str, Any]:
    """Proposition 5: reward of one policy as one knob rises (other knobs fixed), with
    the per-seed count of steps where reward went up."""
    e = get_env(env)
    pol = e.policies()[policy]
    levels = (0.0, 0.25, 0.5, 0.75, 1.0)
    per: dict[float, list[float]] = {}
    vid = e.default_verifier
    for lv in levels:
        ct = base if lv == 0 else f"{base};{knob}={lv}"
        rows = pmap(_row, [(env, pol, s, ct) for s in seeds], workers=workers)
        per[lv] = [r[vid][0] for r in rows]
    ups = sum(
        per[b][i] > per[a][i] + 1e-12
        for a, b in itertools.pairwise(levels)
        for i in range(len(seeds))
    )
    return {
        "env": env,
        "policy": policy,
        "knob": knob,
        "verifier": vid,
        "mean": {f"{lv:g}": round(sum(v) / len(v), 4) for lv, v in per.items()},
        "ci95": {f"{lv:g}": [round(x, 4) for x in bootstrap_ci(v)[1:]] for lv, v in per.items()},
        "per_seed_increases": ups,
        "steps": (len(levels) - 1) * len(seeds),
    }


__all__ = [
    "BINS",
    "anchoring_check",
    "baselines",
    "bootstrap_ci",
    "caught",
    "climb",
    "difficulty_of",
    "incentives",
    "play_many",
    "proxy",
    "spearman",
    "sweep",
    "wilson",
]
